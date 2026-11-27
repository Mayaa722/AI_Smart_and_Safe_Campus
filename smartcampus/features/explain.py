"""Feature: Explanation (tutor chat), including quiz follow-ups.

Flow per turn: query prep (step 8) -> retrieval (9) -> gate (10) -> generation (11, prompt A or B)
-> code checks + R0 (12/13) -> optional C1 claim check + R1 repair (12/13) -> history update.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field

from .. import prompts, schemas
from ..checks import explanation_findings
from ..evidence import feature_input
from ..llm import QuotaExceeded
from ..query import format_history, prepare_queries
from ..repair import format_repair, repair_explanation
from ..verify import claim_check, claim_findings, claims_flagged

MODES = ("direct", "hint", "socratic", "practice")
NOT_COVERED = ("I couldn't find this in the lecture slides, so I won't guess. "
               "Try rephrasing your question, or ask about a topic from the lecture.")


GATE_UNSURE_NOTE = ("<search_note>The search found no slide that clearly matches this question. Answer only "
                    "if the slides below really cover it; otherwise say so and set covered_by_lecture to \"no\"."
                    "</search_note>")


def best_dense(retrieval: dict) -> float:
    return max((part.get("top_dense") or 0.0) for part in (retrieval.get("per_question") or [retrieval]))


def gate_retry(lecture, message: str, history: list[dict], retrieval: dict):
    """The gate abstained. Reworded questions score low with the reranker even when search found the
    right slides (2026-11-26 scan: 5 of 115 lecture questions were refused). Unless the question is
    clearly unrelated, rewrite it into the lecture's own terms and search again; if the gate is still
    not passed, let the generator decide with the slides found ("unsure").
    Returns (outcome, rewritten prepared queries or None, retrieval to use, log)."""
    r = lecture.settings["retrieval"]
    log = {"first_top_rerank": retrieval.get("top_rerank"), "first_top_dense": round(best_dense(retrieval), 4)}
    if not r.get("gate_retry", True) or best_dense(retrieval) < r.get("gate_unsure_min_dense", 0.40):
        return "refuse", None, retrieval, {**log, "outcome": "refuse"}
    rewritten = prepare_queries(lecture.llm("query"), lecture.document, message, history, force=True)
    log["rewrite"] = {"used_model": rewritten.get("used_model"), "error": rewritten.get("error"),
                      "queries": [q["search_query"] for q in rewritten.get("questions", [])]}
    if rewritten.get("used_model") and rewritten["needs_lecture"] and rewritten["questions"]:
        again = lecture.retriever.search_many([q["search_query"] for q in rewritten["questions"]])
        log.update(second_top_dense=round(best_dense(again), 4),
                   second_abstain=again["abstain"], second_abstain_reason=again["abstain_reason"])
        outcome = "unsure" if again["abstain"] else "passed_after_rewrite"
        return outcome, rewritten, again, {**log, "outcome": outcome}
    return "unsure", None, retrieval, {**log, "outcome": "unsure"}


@dataclass
class ExplanationSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    history: list[dict] = field(default_factory=list)   # {"role": "student"|"tutor", "content", "mode"}
    quiz_context: dict | None = None                     # set for "Ask about this question" chats
    quiz_slide_ids: list[str] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)


def answer(lecture, session: ExplanationSession, message: str, mode: str = "direct",
           version: str | None = None, verify: bool | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode}")
    if not message.strip():
        raise ValueError("Write a question first.")
    settings = lecture.settings
    version = version or settings.get("prompt_version", "A")
    generator = lecture.llm("generator")
    history_text = format_history(session.history, settings.get("history_turns", 6))

    # Step 8: standalone search queries
    prepared = prepare_queries(lecture.llm("query"), lecture.document, message, session.history, session.quiz_context)
    turn = {"message": message, "mode": mode, "version": version, "model": generator.spec["model"],
            "query_prep": prepared}

    # Steps 9-10: retrieval and gate
    retrieval, selected_ids = None, []
    if prepared["needs_lecture"]:
        retrieval = lecture.retriever.search_many([q["search_query"] for q in prepared["questions"]])
        selected_ids = [s["source_id"] for s in retrieval["selected"]]
    if session.quiz_slide_ids:
        cap = settings["retrieval"].get("max_evidence_slides", 12)
        selected_ids = list(dict.fromkeys(session.quiz_slide_ids + selected_ids))[:cap]
    turn["retrieval"] = retrieval
    gate_unsure = False
    if prepared["needs_lecture"] and retrieval and retrieval["abstain"] and not session.quiz_context:
        outcome, rewritten, retrieval, log = gate_retry(lecture, message, session.history, retrieval)
        turn["gate_retry"] = log
        if outcome == "refuse":
            output = {"answer": NOT_COVERED, "citations": [], "covered_by_lecture": "no", "follow_up": ""}
            return _finish(session, turn, output, mode, status="not_covered_gate")
        if rewritten:
            prepared = rewritten
        turn["retrieval"] = retrieval
        selected_ids = [s["source_id"] for s in retrieval["selected"]]
        gate_unsure = outcome == "unsure"

    slides = lecture.pages(selected_ids)
    images = lecture.images_for(generator, slides)
    extra = []
    if gate_unsure:
        extra.append(GATE_UNSURE_NOTE)
    if any(not q.get("resolved", True) for q in prepared["questions"]):
        extra.append("<search_note>A reference in the student's message could not be resolved from the "
                     "conversation; ask the student to clarify if needed.</search_note>")
    task = prompts.render(prompts.get(f"explain_{version}"), mode=mode)
    user = feature_input(lecture.name, slides, task, [sid for sid, _ in images], history_text,
                         session.quiz_context, message, extra_blocks=extra)
    base_user = user[: user.rfind(task)].rstrip()
    schema = schemas.explanation(version)
    response = generator.generate(prompts.get("system"), user, schema=schema, images=images,
                                  temperature=settings.get("temperature", 0.0), task=f"explain_{mode}")
    turn["generation"] = _meta(response)
    if not response.ok:
        return _finish(session, turn, None, mode, status="error", error=response.error)
    output = response.parsed
    turn["raw_output"] = output  # kept for "before repair" metrics

    # Step 12-13: code checks, one format repair
    allowed = {s["source_id"] for s in slides}
    by_id = {s["source_id"]: s for s in slides}
    n_questions = max(1, len(prepared["questions"]))
    found = explanation_findings(output, allowed, by_id, mode, n_questions, version)
    if found:
        repaired, log = format_repair(generator, base_user, output, found, schema, images)
        turn["format_repair"] = log
        if repaired and len(explanation_findings(repaired, allowed, by_id, mode, n_questions, version)) < len(found):
            output = repaired
        turn["code_findings"] = explanation_findings(output, allowed, by_id, mode, n_questions, version)

    # Step 12-13: claim check (sampled) and one content repair
    vcfg = settings.get("verify", {})
    if output["covered_by_lecture"] == "no":
        # A refusal ("the slides do not cover X") cannot be supported by slides, so the claim checker would
        # mark it unfaithful and repair it (2026-11-27 test run). Never check it, even when a check is forced;
        # the benchmark scores refusals by correct abstention instead.
        return _finish(session, turn, output, mode, status="not_covered", slide_ids=[s["source_id"] for s in slides])
    do_verify = verify if verify is not None else (
        vcfg.get("enabled", True) and slides
        and (output["covered_by_lecture"] == "partly" or random.random() < vcfg.get("tutor_sample_rate", 0.25)))
    status = "unchecked"
    if do_verify:
        checker = lecture.llm("checker")
        try:
            check = claim_check(checker, lecture.name, slides, output["answer"], "explanation")
            turn["claim_check"] = check
            if not check.get("ok"):
                turn["check_error"] = check.get("error")
            else:
                status = "verified"
                if claims_flagged(check, vcfg.get("faithfulness_threshold", 0.9)):
                    repaired, log = repair_explanation(generator, base_user, output, claim_findings(check), mode, version, images)
                    turn["content_repair"] = log
                    status = "unreviewed"
                    if repaired:
                        output = repaired
                        recheck = claim_check(checker, lecture.name, slides, output["answer"], "explanation")
                        turn["claim_recheck"] = recheck
                        if recheck.get("ok") and not claims_flagged(recheck, vcfg.get("faithfulness_threshold", 0.9)):
                            status = "verified"
        except QuotaExceeded as exc:  # keep the answer; say it was not (fully) checked
            turn["check_error"] = str(exc)
            status = "unchecked" if status == "unchecked" else "unreviewed"
    return _finish(session, turn, output, mode, status=status, slide_ids=[s["source_id"] for s in slides])


def _finish(session, turn, output, mode, status, error=None, slide_ids=None):
    turn.update(output=output, status=status, error=error, evidence_slides=slide_ids or [])
    if output is not None:
        session.history.append({"role": "student", "content": turn["message"], "mode": mode})
        session.history.append({"role": "tutor", "content": output["answer"], "mode": mode})
    session.turns.append(turn)
    return turn


def _meta(response) -> dict:
    return {"ok": response.ok, "error": response.error, "seconds": response.seconds,
            "waited_seconds": response.waited_seconds, "usage": response.usage, "cached": response.cached,
            "model": response.model}


def quiz_followup_session(question: dict, chosen_index: int) -> ExplanationSession:
    """Chat about one graded quiz question (only created after the quiz is submitted)."""
    letters = "ABCD"
    context = {"stem": question["stem"],
               "options": {letters[i]: o for i, o in enumerate(question["options"])},
               "correct_option": letters[question["answer_index"]],
               "student_choice": letters[chosen_index] if chosen_index is not None else None,
               "explanation": question["explanation"], "slide_ids": question["citations"]}
    return ExplanationSession(quiz_context=context, quiz_slide_ids=list(question["citations"]))
