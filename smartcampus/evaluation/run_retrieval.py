"""M1 benchmark: retrieval ablation ladder on a test set (no generation calls, except the optional
query rewriting mode and the one-off slide descriptions)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import CONFIG_DIR, RUNS_DIR, load_settings
from ..lecture import Lecture
from ..query import prepare_queries
from .metrics import by_group, hit_at_k, ndcg_at_k, recall_at_k, reciprocal_rank, summarize
from .testset import load_testset

KS = (1, 3, 4, 6, 10)


def load_ablations() -> dict:
    data = json.loads((CONFIG_DIR / "ablations.json").read_text(encoding="utf-8"))
    return {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in data.items() if not k.startswith("_")}


def queries_for(question: dict, mode: str, lecture: Lecture) -> list[str]:
    history = question.get("history", [])
    if mode == "concat":  # the teammate's method: previous student messages + the new one
        previous = [t["content"] for t in history if t["role"] == "student"][-2:]
        return [" ".join(previous + [question["question"]])]
    if mode == "rewrite":
        prepared = prepare_queries(lecture.llm("query"), lecture.document, question["question"], history)
        return [q["search_query"] for q in prepared["questions"]] or [question["question"]]
    return [question["question"]]


def run(testset_path: str, configs: list[str] | None = None, query_mode: str = "raw", progress=print) -> dict:
    testset = load_testset(testset_path)
    ablations = load_ablations()
    configs = configs or list(ablations)
    base = Lecture.from_file(testset["_lecture_path"], load_settings(), testset.get("lecture_name"), progress=progress)
    page_of = {p["source_id"]: p["page"] for p in base.document["pages"]}
    report = {"testset": testset["_path"], "lecture": base.name, "doc_id": base.doc_id, "query_mode": query_mode,
              "created_at": datetime.now(timezone.utc).isoformat(), "configs": {}}

    for name in configs:
        settings = load_settings({"retrieval": ablations[name]})
        lecture = Lecture(base.document, settings)
        progress(f"[{name}] indexing / searching {len(testset['questions'])} questions")
        rows = []
        for q in testset["questions"]:
            result = lecture.retriever.search_many(queries_for(q, query_mode, lecture))
            per_q = result.get("per_question", [result])
            ranked_ids: list[str] = []
            core = [s["source_id"] for s in result["selected"] if s["reason"] == "retrieved"]
            for sid in core:
                ranked_ids.append(sid)
            for depth in range(max(len(r["ranked_pages"]) for r in per_q)):
                for r in per_q:
                    if depth < len(r["ranked_pages"]) and r["ranked_pages"][depth] not in ranked_ids:
                        ranked_ids.append(r["ranked_pages"][depth])
            ranked = [page_of[s] for s in ranked_ids]
            evidence = [page_of[s["source_id"]] for s in result["selected"]]
            gold = q.get("gold_pages", [])
            row = {"id": q["id"], "type": q.get("type", "other"), "gold": gold, "ranked": ranked[:15],
                   "evidence": evidence, "abstained": result["abstain"], "seconds": result["seconds"],
                   "mrr": reciprocal_rank(ranked, gold) if gold else None,
                   "evidence_recall": recall_at_k(evidence, gold, len(evidence)) if gold else None,
                   "ndcg@k": ndcg_at_k(ranked, gold, settings["retrieval"]["top_k"]) if gold else None}
            for k in KS:
                row[f"recall@{k}"] = recall_at_k(ranked, gold, k) if gold else None
                row[f"hit@{k}"] = hit_at_k(ranked, gold, k) if gold else None
            if q.get("type") == "out_of_scope":
                row["abstain_correct"] = float(result["abstain"])
            else:
                row["false_abstain"] = float(result["abstain"])
            rows.append(row)
        metrics = ["mrr", "evidence_recall", "ndcg@k"] + [f"recall@{k}" for k in KS] + [f"hit@{k}" for k in KS]
        report["configs"][name] = {
            "settings": ablations[name],
            "overall": {m: summarize([r.get(m) for r in rows]) for m in metrics + ["abstain_correct", "false_abstain", "seconds"]},
            "by_type": {m: by_group(rows, "type", m) for m in ("recall@4", "recall@6", "mrr", "evidence_recall")},
            "misses": [{"id": r["id"], "gold": r["gold"], "ranked": r["ranked"][:6]} for r in rows
                       if r["gold"] and not set(r["gold"]) & set(r["evidence"])],
            "rows": rows}
    out = RUNS_DIR / "eval" / f"retrieval_{base.doc_id}_{query_mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    report["saved_to"] = str(out.with_suffix(".md"))
    return report


def markdown(report: dict) -> str:
    lines = [f"# Retrieval benchmark (M1): {report['lecture']}", "",
             f"Test set: `{report['testset']}` · query mode: {report['query_mode']} · {report['created_at']}", "",
             "| Config | n | Recall@4 | Recall@6 | Recall@10 | MRR | Evidence recall | Abstain on out-of-scope | False abstain | s/query |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    fmt = lambda s: "–" if s["mean"] is None else f"{s['mean']:.3f}"
    for name, c in report["configs"].items():
        o = c["overall"]
        lines.append(f"| {name} | {o['recall@4']['n']} | {fmt(o['recall@4'])} | {fmt(o['recall@6'])} | {fmt(o['recall@10'])} | "
                     f"{fmt(o['mrr'])} | {fmt(o['evidence_recall'])} | {fmt(o['abstain_correct'])} ({o['abstain_correct']['n']}) | "
                     f"{fmt(o['false_abstain'])} | {fmt(o['seconds'])} |")
    lines += ["", "## Recall@4 by question type", ""]
    types = sorted({t for c in report["configs"].values() for t in c["by_type"]["recall@4"]})
    lines += ["| Config | " + " | ".join(types) + " |", "|---|" + "---|" * len(types)]
    for name, c in report["configs"].items():
        cells = [f"{fmt(c['by_type']['recall@4'][t])} (n={c['by_type']['recall@4'][t]['n']})" if t in c["by_type"]["recall@4"] else "–"
                 for t in types]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
