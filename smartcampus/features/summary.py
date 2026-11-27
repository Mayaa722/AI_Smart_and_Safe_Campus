"""Feature: lecture summary from the ORIGINAL slides (+ labelled descriptions), not model notes.

If the lecture does not fit the evidence budget, each slide's text is shortened evenly (same rule
for every model, set by settings.summary.max_evidence_chars; 130000 since 2026-11-26 so that no test
lecture is cut) instead of dropping slides. A summary must cite every quarter of the lecture.
"""
from __future__ import annotations

import copy

from .. import prompts, schemas
from ..checks import summary_findings
from ..evidence import evidence_block, feature_input
from ..llm import QuotaExceeded
from ..repair import format_repair, repair_summary
from ..verify import claim_check, claim_findings, claims_flagged


def fit_slides(lecture_name: str, slides: list[dict], budget: int) -> tuple[list[dict], bool]:
    if len(evidence_block(lecture_name, slides)) <= budget:
        return slides, False
    trimmed = copy.deepcopy(slides)
    cap = max(len(s.get("text", "")) for s in trimmed)
    while cap > 200 and len(evidence_block(lecture_name, trimmed)) > budget:
        cap = int(cap * 0.85)
        for s, original in zip(trimmed, slides):
            s["text"] = original.get("text", "")[:cap]
            if s.get("description"):
                d = original["description"]
                s["description"] = {**d, "description": d.get("description", "")[: cap // 2],
                                    "transcription": d.get("transcription", "")[: cap // 2]}
    return trimmed, True


def summarize(lecture, version: str | None = None, verify: bool | None = None) -> dict:
    settings = lecture.settings
    version = version or settings.get("prompt_version", "A")
    generator = lecture.llm("generator")
    content = lecture.content_pages()
    content_set = {p["source_id"] for p in content}
    skipped = [p["source_id"] for p in lecture.document["pages"] if p["source_id"] not in content_set]
    slides, trimmed = fit_slides(lecture.name, content, settings["summary"]["max_evidence_chars"])
    task = prompts.get(f"summary_{version}")
    user = feature_input(lecture.name, slides, task)
    base_user = user[: user.rfind(task)].rstrip()
    schema = schemas.summary(version)
    record = {"feature": "summary", "version": version, "model": generator.spec["model"],
              "trimmed_evidence": trimmed, "skipped_non_content": skipped}
    response = generator.generate(prompts.get("system"), user, schema=schema,
                                  temperature=settings.get("temperature", 0.0), task="summary")
    record["generation"] = {"ok": response.ok, "error": response.error, "seconds": response.seconds,
                            "usage": response.usage, "cached": response.cached}
    if not response.ok:
        record.update(status="error", output=None)
        lecture.save_output("summary", record)
        return record
    output = response.parsed
    record["raw_output"] = output  # kept for "before repair" metrics
    allowed = {p["source_id"] for p in lecture.document["pages"]}
    content_ids = [p["source_id"] for p in content]
    found = summary_findings(output, allowed, content_ids, version)
    if found:
        repaired, log = format_repair(generator, base_user, output, found, schema)
        record["format_repair"] = log
        if repaired and len(summary_findings(repaired, allowed, content_ids, version)) < len(found):
            output = repaired
        record["code_findings"] = summary_findings(output, allowed, content_ids, version)

    status = "unchecked"
    vcfg = settings.get("verify", {})
    do_verify = vcfg.get("enabled", True) if verify is None else verify
    if do_verify:
        checker = lecture.llm("checker")
        try:
            check = claim_check(checker, lecture.name, slides, output["body"], "summary")
            record["claim_check"] = check
            if not check.get("ok"):
                record["check_error"] = check.get("error")
            else:
                status = "verified"
                if claims_flagged(check, vcfg.get("faithfulness_threshold", 0.9)):
                    repaired, log = repair_summary(generator, base_user, output, claim_findings(check), version)
                    record["content_repair"] = log
                    status = "unreviewed"
                    if repaired:
                        output = repaired
                        recheck = claim_check(checker, lecture.name, slides, output["body"], "summary")
                        record["claim_recheck"] = recheck
                        if recheck.get("ok") and not claims_flagged(recheck, vcfg.get("faithfulness_threshold", 0.9)):
                            status = "verified"
        except QuotaExceeded as exc:  # keep the summary; say it was not (fully) checked
            record["check_error"] = str(exc)
            status = "unchecked" if status == "unchecked" else "unreviewed"
    record.update(status=status, output=output, evidence_slides=[s["source_id"] for s in slides])
    record["saved_to"] = str(lecture.save_output("summary", record))
    return record
