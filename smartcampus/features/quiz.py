"""Feature: quiz generated per topic from the ORIGINAL slides of that topic.

Generation -> code checks + R0 -> per question: two blind solvers (C3) + item review (C4)
-> one repair (R3) -> re-check -> verified / withheld. Then answer positions are balanced.
The student view never contains keys; grading is done by code after the whole quiz is submitted.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re

from .. import prompts, schemas
from ..checks import question_findings, quiz_findings
from ..evidence import feature_input
from ..llm import QuotaExceeded
from ..repair import format_repair, repair_question, replacement_question
from ..structure import topic_groups
from ..text import squash
from ..verify import blind_solve, quiz_item_valid, review_item

LETTERS = "ABCD"


def _split_counts(groups: list[list[dict]], n: int) -> list[int]:
    sizes = [len(g) for g in groups]
    counts = [max(1, round(n * s / sum(sizes))) for s in sizes]
    while sum(counts) > n:
        counts[counts.index(max(counts))] -= 1
    while sum(counts) < n:
        counts[sizes.index(max(sizes))] += 1
    return counts


def generate_group(lecture, slides: list[dict], count: int, version: str, topic: str | None = None):
    """Write `count` questions from one group of slides: generation + code checks + R0.
    Returns (items, part log). Items are unchecked; see check_item. Also used by the question bank."""
    settings = lecture.settings
    generator = lecture.llm("generator")
    schema = schemas.quiz(version)
    task = prompts.render(prompts.get(f"quiz_{version}"), n=count, topic=topic or "")
    user = feature_input(lecture.name, slides, task)
    base_user = user[: user.rfind(task)].rstrip()
    response = generator.generate(prompts.get("system"), user, schema=schema,
                                  temperature=settings.get("temperature", 0.0), task="quiz")
    part = {"slides": [s["source_id"] for s in slides], "requested": count,
            "generation": {"ok": response.ok, "error": response.error, "seconds": response.seconds,
                           "usage": response.usage, "cached": response.cached}}
    if not response.ok:
        return [], part
    output = response.parsed
    allowed = {s["source_id"] for s in slides}
    by_id = {s["source_id"]: s for s in slides}
    found = quiz_findings(output, allowed, by_id, version)
    if found:
        repaired, log = format_repair(generator, base_user, output, found, schema)
        part["format_repair"] = log
        if repaired and len(quiz_findings(repaired, allowed, by_id, version)) < len(found):
            output = repaired
    part["notes"] = output.get("notes", "")
    blueprint = output.get("blueprint", [])
    items = [{"question": q, "blueprint_item": blueprint[i] if i < len(blueprint) else None,
              "group_slides": part["slides"], "base_user": base_user, "code_issues": question_findings(q, allowed)}
             for i, q in enumerate(output["questions"])]
    return items, part


def check_item(lecture, item: dict, items: list[dict], version: str, replace: bool = True) -> dict | None:
    """Full check of one item in place (checks, one repair, re-check), then, if it ends up withheld,
    one replacement question (R4). Returns the replacement item or None. QuotaExceeded propagates,
    so a caller can redo the whole unit later (cached calls cost nothing)."""
    item["status"] = "withheld" if item["code_issues"] else "unchecked"
    if item["status"] != "withheld":
        _verify_item(lecture, item, items, version)
    if replace and item["status"] == "withheld":
        return _replacement(lecture, item, items, version)
    return None


def generate(lecture, n: int | None = None, version: str | None = None, topic: str | None = None,
             verify: bool | None = None) -> dict:
    settings = lecture.settings
    n = n or settings["quiz"]["default_questions"]
    version = version or settings.get("prompt_version", "A")
    generator = lecture.llm("generator")
    if topic:
        found = lecture.retriever.search(topic, top_k=max(4, n))
        groups = [lecture.pages([s["source_id"] for s in found["selected"]])]
        counts = [n]
    else:
        groups = topic_groups(lecture.content_pages(), math.ceil(n / 2))
        counts = _split_counts(groups, n)

    items, parts = [], []
    for slides, count in zip(groups, counts):
        if count < 1 or not slides:
            continue
        new_items, part = generate_group(lecture, slides, count, version, topic)
        parts.append(part)
        items += new_items

    # drop exact duplicate stems across topic groups
    seen, unique = set(), []
    for item in items:
        key = squash(item["question"]["stem"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    items = unique

    vcfg = settings.get("verify", {})
    do_verify = vcfg.get("enabled", True) if verify is None else verify
    quota_error = None
    for item in items:
        item["status"] = "unchecked"
        if item["code_issues"]:
            item["status"] = "withheld"
        if do_verify and item["status"] != "withheld":
            if quota_error:  # a checker's daily limit was reached: keep the item, mark it unchecked
                item["check_error"] = quota_error
                continue
            try:
                _verify_item(lecture, item, items, version)
            except QuotaExceeded as exc:  # first check or recheck could not run: "unchecked" either way
                quota_error = item["check_error"] = str(exc)
                item["status"] = "unchecked"

    # R4: one checked replacement per withheld question, so the quiz keeps its requested length
    if do_verify and settings["quiz"].get("replace_withheld", True):
        for item in [it for it in items if it["status"] == "withheld"]:
            if quota_error:
                break
            try:
                new = _replacement(lecture, item, items, version)
            except QuotaExceeded as exc:
                quota_error = str(exc)
                item["replacement_error"] = quota_error
                break
            if new:
                items.insert(items.index(item) + 1, new)

    released =[it for it in items if it["status"] in ("verified", "unchecked")]
    _balance_positions(released, seed=settings["quiz"].get("seed", 42))
    quiz = {"quiz_id": hashlib.sha256(json.dumps([it["question"] for it in released], sort_keys=True).encode()).hexdigest()[:12],
            "feature": "quiz", "version": version, "model": generator.spec["model"], "topic": topic,
            "requested": n, "questions": [it["question"] for it in released],
            "item_status": [it["status"] for it in released],
            "withheld": [{"question": it["question"], "status": it["status"], "checks": it.get("checks")}
                         for it in items if it not in released],
            "items": [{k: v for k, v in it.items() if k != "base_user"} for it in items], "parts": parts}
    quiz["saved_to"] = str(lecture.save_output("quiz", quiz))
    return quiz


def _check_slides(lecture, item: dict, question: dict) -> list[dict]:
    """Checkers see only the slides the question cites, so the key must follow from its own citations
    (this also tests citation accuracy). Falls back to the topic group if no cited slide is found."""
    return lecture.pages(question.get("citations") or []) or lecture.pages(item["group_slides"])


def _apply_bloom_review(item: dict, review: dict) -> None:
    """Record the reviewer's assessed Bloom level as the label when it disagrees with the stated one.
    This only corrects the label; it never changes the verdict or the item's status."""
    q, actual = item["question"], review.get("bloom_level_actual")
    if review.get("ok") and review.get("bloom_matches") is False and actual and actual != q.get("bloom_level"):
        item.setdefault("bloom_relabels", []).append({"from": q.get("bloom_level"), "to": actual,
                                                      "by": review.get("model")})
        q["bloom_level"] = actual


def _withheld_reasons(item: dict) -> list[str]:
    final = item.get("recheck") or item.get("checks") or {}
    reasons = [f"code check: {f}" for f in item.get("code_issues") or []]
    reasons += [f"blind solver {s.get('model')}: verdict {s.get('verdict')}, key not confirmed"
                for s in final.get("solves", []) if s.get("ok") and not s.get("key_confirmed")]
    review = final.get("review") or {}
    reasons += [f"option {d['option']} {d['label']}: {d['reason']}" for d in review.get("distractor_quality", [])
                if d["label"] != "plausible"]
    reasons += list(review.get("fixes") or [])
    if (item.get("repair") or {}).get("repair_notes"):
        reasons += [f"repair: {n['action']} - {n['reason']}" for n in item["repair"]["repair_notes"]
                    if n["action"] == "removed"]
    return reasons or ["failed the quiz checks"]


def _replacement(lecture, item: dict, items: list[dict], version: str) -> dict | None:
    """Write and check a NEW question in place of a withheld one. The replacement is treated like any
    other question (checks, one repair, re-check); if it is still withheld, no further replacement is tried."""
    others = [it["question"]["stem"] for it in items if it is not item]
    withheld = {"question": item["question"], **({"blueprint_item": item["blueprint_item"]} if version == "B" else {})}
    out, log = replacement_question(lecture.llm("generator"), item["base_user"], withheld,
                                    _withheld_reasons(item), others, version)
    item["replacement"] = log
    if not out or out["removed"]:
        return None
    allowed = set(item["group_slides"])
    new = {"question": out["question"], "blueprint_item": out.get("blueprint_item"), "group_slides": item["group_slides"],
           "base_user": item["base_user"], "code_issues": question_findings(out["question"], allowed),
           "replaces": item["question"]["stem"], "status": "unchecked"}
    if new["code_issues"] or squash(new["question"]["stem"]) in {squash(s) for s in others + [item["question"]["stem"]]}:
        new["status"] = "withheld"
        return new
    _verify_item(lecture, new, items + [new], version)
    return new


def _verify_item(lecture, item: dict, items: list[dict], version: str) -> None:
    checker, solver2, generator = lecture.llm("checker"), lecture.llm("solver_2"), lecture.llm("generator")
    allowed = set(item["group_slides"]) | set(item["question"]["citations"])  # what the generator could cite

    def run_checks(question):
        slides = _check_slides(lecture, item, question)
        solves = [blind_solve(checker, lecture.name, slides, question, seed=1),
                  blind_solve(solver2, lecture.name, slides, question, seed=2)]
        review = review_item(checker, lecture.name, slides, question)
        return solves, review

    def errors(solves, review):
        return [str(c.get("error")) for c in solves + [review] if not c.get("ok")]

    solves, review = run_checks(item["question"])
    item["checks"] = {"solves": solves, "review": review}
    _apply_bloom_review(item, review)
    failed = errors(solves, review)
    if failed:  # a checker errored (not a verdict): do not repair on missing evidence
        item["check_error"] = "; ".join(failed)
        return
    if quiz_item_valid(solves, review):
        item["status"] = "verified"
        return
    findings =[{"issue": "key not confirmed by blind solver", "details": f"{s.get('model')}: verdict {s.get('verdict')}, "
                 f"best option {s.get('best_index')}"} for s in solves if not s.get("key_confirmed")]
    findings += [{"issue": "item review", "details": fix} for fix in (review.get("fixes") or [])]
    others = [it["question"]["stem"] for it in items if it is not item]
    original = {"question": item["question"], **({"blueprint_item": item["blueprint_item"]} if version == "B" else {})}
    repaired, log = repair_question(generator, item["base_user"], original, findings,
                                    {"blind_solver_1": solves[0].get("raw"), "blind_solver_2": solves[1].get("raw"),
                                     "review": review}, others, version)
    item["repair"] = log
    if not repaired or repaired["removed"] or question_findings(repaired["question"], allowed):
        item["status"] = "withheld"
        return
    item["original_question"], item["question"] = item["question"], repaired["question"]
    if repaired.get("blueprint_item"):
        item["blueprint_item"] = repaired["blueprint_item"]
    solves, review = run_checks(item["question"])
    item["recheck"] = {"solves": solves, "review": review}
    _apply_bloom_review(item, review)
    failed = errors(solves, review)
    if failed:  # same rule as the first check: an errored checker is "not checked", not a failed verdict
        item["check_error"] = "; ".join(failed)
        item["status"] = "unchecked"
        return
    item["status"] = "verified" if quiz_item_valid(solves, review) else "withheld"


def _balance_positions(items: list[dict], seed: int) -> None:
    """Spread correct answers evenly over A-D; keep each rationale attached to its option."""
    rng = random.Random(seed)
    targets = [i % 4 for i in range(len(items))]
    rng.shuffle(targets)
    for item, target in zip(items, targets):
        q = item["question"]
        if len(q["options"]) != 4:
            continue
        key = q["answer_index"]
        wrong = [i for i in range(4) if i != key]
        rationales = dict(zip(wrong, q["distractor_rationales"]))
        rng.shuffle(wrong)
        order = wrong[:]
        order.insert(target, key)
        mapping = {LETTERS[old]: LETTERS[new] for new, old in enumerate(order)}
        q["options"] = [q["options"][i] for i in order]
        q["distractor_rationales"] = [rationales[i] for i in order if i != key]
        q["answer_index"] = target
        q["explanation"] = re.sub(r"\b([Oo]ption|[Cc]hoice|[Aa]nswer)\s+([ABCD])\b",
                                  lambda m: f"{m.group(1)} {mapping[m.group(2)]}", q["explanation"])


def student_view(quiz: dict) -> list[dict]:
    """What the browser receives before submission: no keys, explanations or citations."""
    return [{"number": i, "stem": q["stem"], "options": q["options"]} for i, q in enumerate(quiz["questions"], 1)]


def grade(quiz: dict, answers: list[int | None]) -> dict:
    questions = quiz["questions"]
    if len(answers) != len(questions) or any(a is None for a in answers):
        raise ValueError(f"Please answer all {len(questions)} questions before submitting.")
    results = []
    for i, (q, chosen) in enumerate(zip(questions, answers), 1):
        correct = chosen == q["answer_index"]
        wrong = [j for j in range(len(q["options"])) if j != q["answer_index"]]
        why_wrong = None if correct else q["distractor_rationales"][wrong.index(chosen)]
        results.append({"number": i, "chosen": chosen, "correct": correct, "answer_index": q["answer_index"],
                        "correct_option": q["options"][q["answer_index"]], "explanation": q["explanation"],
                        "why_your_choice_is_wrong": why_wrong, "citations": q["citations"],
                        "bloom_level": q["bloom_level"]})
    score = sum(r["correct"] for r in results)
    return {"score": score, "total": len(results), "items": results}
