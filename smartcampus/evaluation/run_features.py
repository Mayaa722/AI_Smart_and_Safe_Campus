"""M2-M4 benchmark: run the features for several generator models (arms) and prompt versions,
then score them with ONE fixed independent judge model.

- Explanation: M2 faithfulness (C1), M3 coverage (C2, needs reviewed key points), mode compliance (C5),
  correct abstention on out-of-scope questions. Scored before and after in-app repair.
- Summary: M2 (C1) and M3 (C2 against the lecture's reviewed key points).
- Quiz: M4 = questions confirmed by two blind solvers (C3) AND passed by the reviewer (C4) / requested.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import RUNS_DIR, load_settings
from ..features import quiz as quiz_feature
from ..features.explain import ExplanationSession, answer
from ..features.summary import summarize
from ..lecture import Lecture
from ..llm import LLMClient, QuotaExceeded
from ..query import format_history
from ..verify import blind_solve, claim_check, coverage_check, mode_check, quiz_item_valid, review_item
from .metrics import by_group, summarize as stats
from .testset import load_testset


def keypoints_path(testset_path: str) -> Path:
    path = Path(testset_path)
    return path.with_name(path.stem + ".keypoints.json")


def load_lecture_keypoints(testset_path: str) -> list[dict] | None:
    path = keypoints_path(testset_path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["key_points"] if data.get("reviewed") else None


def run(testset_path: str, arms: list[str], versions: tuple[str, ...] = ("A", "B"),
        features: tuple[str, ...] = ("explain", "summary", "quiz"), repeats: int = 1,
        app_verify: bool = True, quiz_questions: int | None = None, progress=print) -> dict:
    testset = load_testset(testset_path)
    settings = load_settings()
    base = Lecture.from_file(testset["_lecture_path"], settings, testset.get("lecture_name"), progress=progress)
    judge = base.llm("judge")
    solver2 = base.llm("solver_2")
    lecture_points = load_lecture_keypoints(testset_path)
    report = {"testset": testset["_path"], "lecture": base.name, "judge": judge.spec["model"],
              "created_at": datetime.now(timezone.utc).isoformat(), "app_verify": app_verify,
              "repeats": repeats, "runs": []}

    for arm in arms:
        for version in versions:
            for repeat in range(repeats):
                generator = LLMClient(arm, cache=(repeats == 1))
                lecture = Lecture(base.document, settings, llms={"generator": generator})
                tag = {"arm": arm, "version": version, "repeat": repeat}
                progress(f"== {arm} · prompts {version} · run {repeat + 1}/{repeats}")
                try:
                    if "explain" in features:
                        report["runs"].append({**tag, "feature": "explain",
                                               "rows": _explain(lecture, testset, version, judge, app_verify, progress)})
                    if "summary" in features:
                        report["runs"].append({**tag, "feature": "summary",
                                               "rows": [_summary(lecture, version, judge, lecture_points, app_verify)]})
                    if "quiz" in features:
                        report["runs"].append({**tag, "feature": "quiz",
                                               "rows": [_quiz(lecture, version, judge, solver2, app_verify, quiz_questions)]})
                except QuotaExceeded as exc:
                    report["runs"].append({**tag, "feature": "stopped", "rows": [], "error": str(exc)})
                    progress(f"Stopped: {exc}")
    report["summary"] = aggregate(report)
    out = RUNS_DIR / "eval" / f"features_{base.doc_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    report["saved_to"] = str(out.with_suffix(".md"))
    return report


def _explain(lecture, testset, version, judge, app_verify, progress) -> list[dict]:
    rows = []
    for q in testset["questions"]:
        session = ExplanationSession(history=[{"role": t["role"], "content": t["content"]} for t in q.get("history", [])])
        history_text = format_history(session.history)
        mode = q.get("mode", "direct")
        turn = answer(lecture, session, q["question"], mode=mode, version=version, verify=app_verify)
        row = {"id": q["id"], "type": q.get("type", "other"), "mode": mode, "status": turn["status"],
               "error": turn.get("error"), "seconds": (turn.get("generation") or {}).get("seconds"),
               "evidence": turn.get("evidence_slides", [])}
        output = turn.get("output")
        if output is None:
            rows.append(row)
            continue
        row["covered_by_lecture"] = output["covered_by_lecture"]
        if q.get("type") == "out_of_scope":
            row["abstain_correct"] = float(output["covered_by_lecture"] == "no")
        slides = lecture.pages(turn.get("evidence_slides", []))
        if slides and output["covered_by_lecture"] != "no":
            final = claim_check(judge, lecture.name, slides, output["answer"], "explanation")
            row["faithfulness"] = final.get("faithfulness")
            row["contradiction_rate"] = final.get("contradiction_rate")
            row["citation_accuracy"] = final.get("citation_accuracy")
            raw = turn.get("raw_output")
            if raw and raw.get("answer") != output["answer"]:
                row["faithfulness_before_repair"] = claim_check(judge, lecture.name, slides, raw["answer"], "explanation").get("faithfulness")
            else:
                row["faithfulness_before_repair"] = row["faithfulness"]
            if q.get("key_points"):
                points = [{"id": f"k{i}", "point": p, "slide_ids": [s["source_id"] for s in slides]}
                          for i, p in enumerate(q["key_points"], 1)] if isinstance(q["key_points"][0], str) else q["key_points"]
                row["coverage"] = coverage_check(judge, lecture.name, slides, points, output["answer"], "explanation").get("coverage")
        if mode != "direct" or q.get("type") in ("out_of_scope", "followup", "multi"):
            check = mode_check(judge, lecture.name, slides, history_text,
                               {"mode": mode, "message": q["question"], "answer": output["answer"],
                                "covered_by_lecture": output["covered_by_lecture"]})
            row["mode_compliance"] = check.get("compliance")
            row["mode_checks"] = check.get("checks")
        rows.append(row)
        progress(f"  {q['id']}: faithfulness={row.get('faithfulness')} status={row['status']}")
    return rows


def _summary(lecture, version, judge, lecture_points, app_verify) -> dict:
    record = summarize(lecture, version, verify=app_verify)
    row = {"status": record["status"], "seconds": record["generation"]["seconds"], "error": record["generation"]["error"]}
    if record.get("output"):
        slides = lecture.pages(record["evidence_slides"])
        final = claim_check(judge, lecture.name, slides, record["output"]["body"], "summary")
        row.update(faithfulness=final.get("faithfulness"), contradiction_rate=final.get("contradiction_rate"),
                   citation_accuracy=final.get("citation_accuracy"))
        if record.get("raw_output") and record["raw_output"]["body"] != record["output"]["body"]:
            row["faithfulness_before_repair"] = claim_check(judge, lecture.name, slides, record["raw_output"]["body"], "summary").get("faithfulness")
        else:
            row["faithfulness_before_repair"] = row["faithfulness"]
        if lecture_points:
            row["coverage"] = coverage_check(judge, lecture.name, lecture.content_pages(), lecture_points,
                                             record["output"]["body"], "summary").get("coverage")
    return row


def _quiz(lecture, version, judge, solver2, app_verify, n) -> dict:
    quiz = quiz_feature.generate(lecture, n, version, verify=app_verify)
    requested = quiz["requested"]
    valid, bloom = 0, []
    for q in quiz["questions"]:
        slides = lecture.pages(q["citations"])
        solves = [blind_solve(judge, lecture.name, slides, q, seed=11), blind_solve(solver2, lecture.name, slides, q, seed=12)]
        review = review_item(judge, lecture.name, slides, q)
        valid += quiz_item_valid(solves, review)
        bloom.append(q["bloom_level"])
    return {"requested": requested, "released": len(quiz["questions"]), "withheld": len(quiz["withheld"]),
            "valid": valid, "quiz_validity": valid / requested if requested else None,
            "quiz_validity_of_released": valid / len(quiz["questions"]) if quiz["questions"] else None,
            "bloom_levels": bloom, "quiz_id": quiz["quiz_id"]}


METRICS = ("faithfulness", "faithfulness_before_repair", "contradiction_rate", "citation_accuracy", "coverage",
           "mode_compliance", "abstain_correct", "quiz_validity", "quiz_validity_of_released", "seconds")


def aggregate(report: dict) -> dict:
    groups: dict[str, list[dict]] = {}
    for run in report["runs"]:
        groups.setdefault(f"{run['feature']}|{run['arm']}|{run['version']}", []).extend(run["rows"])
    result = {}
    for key, rows in groups.items():
        result[key] = {m: stats([r.get(m) for r in rows]) for m in METRICS}
        result[key]["errors"] = sum(1 for r in rows if r.get("error"))
        result[key]["rows"] = len(rows)
        if key.startswith("explain"):
            result[key]["faithfulness_by_type"] = by_group(rows, "type", "faithfulness")
    return result


def markdown(report: dict) -> str:
    fmt = lambda s: "–" if not s or s["mean"] is None else f"{s['mean']:.3f} ± {s['std']:.3f} (n={s['n']})"
    lines = [f"# Feature benchmark: {report['lecture']}", "",
             f"Judge: {report['judge']} · repeats: {report['repeats']} · in-app verification: {report['app_verify']} · {report['created_at']}", ""]
    for feature, metrics in [("explain", ["faithfulness", "faithfulness_before_repair", "contradiction_rate", "citation_accuracy",
                                          "coverage", "mode_compliance", "abstain_correct", "seconds"]),
                             ("summary", ["faithfulness", "faithfulness_before_repair", "contradiction_rate", "coverage", "seconds"]),
                             ("quiz", ["quiz_validity", "quiz_validity_of_released"])]:
        keys = [k for k in report["summary"] if k.startswith(feature + "|")]
        if not keys:
            continue
        lines += [f"## {feature}", "", "| Arm | Prompts | " + " | ".join(metrics) + " | errors |",
                  "|---|---|" + "---|" * (len(metrics) + 1)]
        for key in sorted(keys):
            _, arm, version = key.split("|")
            s = report["summary"][key]
            lines.append(f"| {arm} | {version} | " + " | ".join(fmt(s[m]) for m in metrics) + f" | {s['errors']}/{s['rows']} |")
        lines.append("")
    return "\n".join(lines)
