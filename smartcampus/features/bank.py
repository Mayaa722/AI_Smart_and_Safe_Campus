"""Quiz question bank: write and check a lecture's questions ONCE, then draw student quizzes from it.

Build (resumable): topic groups -> generate_group (same prompts and code checks as the on-demand
quiz) -> near-duplicate filter (embedding similarity) -> check_item per question (C3 x2 + C4, one
repair, re-check, one replacement). The bank is saved after every step, so a daily limit only pauses
it; the next build continues, and cached model calls cost nothing.

Draw: verified questions only, optional topic filter, spread over topic groups and Bloom levels,
no repeats within a session (until the bank runs out), answer letters shuffled per quiz.
The on-demand quiz (quiz.generate) and the benchmark are unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from datetime import datetime, timezone

import numpy as np

from .. import prompts
from ..index import embedder
from ..llm import QuotaExceeded
from ..structure import topic_groups
from ..text import restore_latex_in
from .quiz import _balance_positions, _split_counts, check_item, generate_group

BANK_FILE = "bank.json"
SERVED = ("verified",)
PROMPTS_USED = ("system", "checker_system", "C3", "C4", "R0", "R3", "R4")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cfg(lecture) -> dict:
    # 0.84 (bge-m3, calculus): real rewordings scored >= 0.848; a chain-rule variant and a different identity
    # scored 0.827-0.830 and were wrongly skipped at 0.82. Provisional: re-check on other lectures.
    return {"per_content_slide": 1.0, "max_items": 30, "min_ready": 3, "near_duplicate_threshold": 0.84,
            **lecture.settings["quiz"].get("bank", {})}


def _model_ids(lecture) -> dict:
    roles = ("generator", "checker", "checker_fallback", "solver_2")
    out = {}
    for role in roles:
        if role == "checker_fallback" and not lecture.settings["models"].get(role):
            continue
        llm = lecture.llm(role) if role != "checker_fallback" else None
        out[role] = (llm.spec.get("model") if llm else lecture.settings["models"][role])
    return out


def fingerprint(lecture, version: str) -> str:
    """Changes when the lecture content, a prompt the bank depends on, or a model changes -> bank is stale."""
    pages = [[p.get("text"), (p.get("description") or {}).get("description"), p.get("section")]
             for p in lecture.document["pages"]]
    data = {"pages": pages, "version": version, "models": _model_ids(lecture),
            "prompts": prompts.prompt_hash(f"quiz_{version}", *PROMPTS_USED)}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def target_size(lecture) -> int:
    cfg = _cfg(lecture)
    return max(1, min(cfg["max_items"], math.ceil(cfg["per_content_slide"] * len(lecture.content_pages()))))


def _path(lecture):
    return lecture.folder / BANK_FILE


def load(lecture) -> dict | None:
    path = _path(lecture)
    # restore_latex_in: fixes questions stored before LaTeX escapes were repaired at parse time
    return restore_latex_in(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None


def _save(lecture, bank: dict) -> None:
    bank["updated"] = _now()
    path = _path(lecture)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(bank, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)  # never leave a half-written bank


def _new_bank(lecture, version: str, fp: str) -> dict:
    target = target_size(lecture)
    groups = topic_groups(lecture.content_pages(), math.ceil(target / 2))
    counts = _split_counts(groups, target)
    return {"doc_id": lecture.doc_id, "lecture": lecture.name, "version": version, "fingerprint": fp,
            "models": _model_ids(lecture), "created": _now(), "target": target, "complete": False,
            "groups": [{"slides": [s["source_id"] for s in g], "requested": c, "generated": False}
                       for g, c in zip(groups, counts)],
            "items": []}


def _item_id(question: dict) -> str:
    return hashlib.sha256(json.dumps(question, sort_keys=True).encode()).hexdigest()[:12]


def _dedup_text(question: dict) -> str:
    return question["stem"] + " " + question["options"][question["answer_index"]]


def _mark_near_duplicates(lecture, bank: dict) -> None:
    """Questions testing the same thing in different words (e.g. two "derivative of tanh" items)
    are marked "duplicate" BEFORE checking, so no quota is spent on them."""
    threshold = _cfg(lecture)["near_duplicate_threshold"]
    for it in bank["items"]:  # threshold raised since: reopen what no longer counts as a duplicate
        if it["status"] == "duplicate" and it.get("similarity", 1.0) < threshold:
            it.update(status="pending", dup_checked=False)
            it.pop("duplicate_of", None)
            it.pop("similarity", None)
    kept =[it for it in bank["items"] if it["status"] != "duplicate" and it.get("dup_checked")]
    new = [it for it in bank["items"] if it["status"] == "pending" and not it.get("dup_checked")]
    if not new:
        return
    model = embedder(lecture.settings["embedding_model"])
    kept_vecs = list(model.encode([_dedup_text(it["question"]) for it in kept])) if kept else []
    new_vecs = model.encode([_dedup_text(it["question"]) for it in new])
    for it, vec in zip(new, new_vecs):
        it["dup_checked"] = True
        if kept_vecs:
            sims = np.asarray(kept_vecs) @ vec
            best = int(np.argmax(sims))
            if sims[best] >= threshold:
                it["status"], it["duplicate_of"] = "duplicate", kept[best]["id"]
                it["similarity"] = round(float(sims[best]), 3)
                continue
        kept.append(it)
        kept_vecs.append(vec)


CHECK_RESULTS = ("checks", "recheck", "repair", "original_question", "check_error", "bloom_relabels",
                 "replacement", "replacement_error")


def _reset_for_recheck(it: dict) -> None:
    """Back to the question as first written, so a retry is the SAME single check-repair-recheck
    path (cached calls replay for free; only the ones that errored run again), never a second repair."""
    previous = {k: it.pop(k) for k in CHECK_RESULTS if k in it}
    it.setdefault("previous_attempts", []).append({"status": it["status"], "check_error": previous.get("check_error")})
    it["question"] = copy.deepcopy(it.get("generated_question") or previous.get("original_question") or it["question"])
    it["status"] = "pending"


def status(lecture, version: str | None = None) -> dict:
    version = version or lecture.settings.get("prompt_version", "A")
    bank = load(lecture)
    if not bank:
        return {"exists": False, "ready": False, "target": target_size(lecture)}
    counts: dict[str, int] = {}
    for it in bank["items"]:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    stale = bank["fingerprint"] != fingerprint(lecture, bank["version"]) or bank["version"] != version
    served = sum(counts.get(s, 0) for s in SERVED)
    return {"exists": True, "stale": stale, "complete": bank["complete"], "version": bank["version"],
            "target": bank["target"], "counts": counts, "served": served,
            "groups_left": sum(not g["generated"] for g in bank["groups"]),
            "ready": not stale and served >= _cfg(lecture)["min_ready"], "updated": bank.get("updated")}


def build(lecture, version: str | None = None, progress=print) -> dict:
    """Build or continue the bank. Returns status(); "stopped" names the reason if a daily limit hit."""
    version = version or lecture.settings.get("prompt_version", "A")
    fp = fingerprint(lecture, version)
    bank = load(lecture)
    if bank and (bank["fingerprint"] != fp or bank["version"] != version):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        _path(lecture).rename(lecture.folder / f"bank_stale_{stamp}.json")  # kept, not deleted
        progress("The lecture, a prompt or a model changed: starting a new bank (old one archived).")
        bank = None
    bank = bank or _new_bank(lecture, version, fp)
    _save(lecture, bank)
    stopped = None
    try:
        # 1) generate per topic group
        for gi, group in enumerate(bank["groups"]):
            if group["generated"]:
                continue
            progress(f"Writing questions for topic group {gi + 1}/{len(bank['groups'])}")
            items, part = generate_group(lecture, lecture.pages(group["slides"]), group["requested"], version)
            group["part"] = part
            if not items:  # non-quota error: try this group again on the next build
                group["last_error"] = part["generation"].get("error")
                continue
            group.update(generated=True, base_user=items[0]["base_user"])
            for it in items:
                it.pop("base_user")
                bank["items"].append({"id": _item_id(it["question"]), "group": gi, "status": "pending",
                                      "generated_question": copy.deepcopy(it["question"]), **it})
            _save(lecture, bank)
        _mark_near_duplicates(lecture, bank)
        for it in bank["items"]:  # a checker errored last time (not a verdict): check again from the start
            if it["status"] == "unchecked":
                _reset_for_recheck(it)
        _save(lecture, bank)

        # 2) check each question (one unit = checks + repair + re-check + replacement)
        pending = [it for it in bank["items"] if it["status"] == "pending"]
        for k, it in enumerate(pending, 1):
            progress(f"Checking question {k}/{len(pending)}")
            base_user = bank["groups"][it["group"]]["base_user"]
            work = {**copy.deepcopy(it), "base_user": base_user}
            others = [{"question": o["question"]} for o in bank["items"]
                      if o is not it and o["status"] != "duplicate"]
            # a replacement is never replaced again; QuotaExceeded -> stop, redo the unit later
            new = check_item(lecture, work, others, version, replace=not it.get("replaces_id"))
            work.pop("base_user")
            it.clear()
            it.update(work)
            if new:
                new.pop("base_user", None)
                first = new.get("original_question") or new["question"]
                bank["items"].append({"id": _item_id(first), "group": it["group"], "replaces_id": it["id"],
                                      "dup_checked": True, "generated_question": copy.deepcopy(first), **new})
            _save(lecture, bank)
    except QuotaExceeded as exc:
        stopped = str(exc)
        bank["last_stop"] = {"at": _now(), "reason": stopped}
        progress(f"Paused: {stopped}\nRun the build again later to continue where it stopped.")
    bank["complete"] = (not stopped and all(g["generated"] for g in bank["groups"])
                        and not any(it["status"] == "pending" for it in bank["items"]))
    _save(lecture, bank)
    return {**status(lecture, version), "stopped": stopped}


def draw(lecture, n: int, topic: str | None = None, exclude: set[str] | frozenset = frozenset(),
         seed: int | None = None) -> dict:
    """A quiz of up to n verified bank questions. Shape matches quiz.generate(), so grading,
    the student view and quiz follow-ups work unchanged. Seen ids in `exclude` are avoided
    until the pool runs out; then repeats are allowed (and counted in "repeats")."""
    bank = load(lecture)
    if not bank:
        raise ValueError("This lecture has no question bank yet. Build it first.")
    pool = [it for it in bank["items"] if it["status"] in SERVED]
    topic_note = None
    if topic:
        found = lecture.retriever.search(topic)
        ids = {s["source_id"] for s in found["selected"]}
        pool = [] if found["abstain"] else [it for it in pool if set(it["question"]["citations"]) & ids]
        topic_note = found.get("abstain_reason") if found["abstain"] else None
    rng = random.Random(seed)
    fresh = [it for it in pool if it["id"] not in exclude]
    seen = [it for it in pool if it["id"] in exclude]
    chosen = _spread(fresh, n, rng)
    repeats = 0
    if len(chosen) < n and seen:  # the student has seen everything new: allow repeats
        extra = _spread(seen, n - len(chosen), rng)
        repeats, chosen = len(extra), chosen + extra
    questions = [copy.deepcopy(it["question"]) for it in chosen]
    _balance_positions([{"question": q} for q in questions], seed=rng.randrange(10**6))
    ids = [it["id"] for it in chosen]
    return {"quiz_id": hashlib.sha256(json.dumps([ids, seed]).encode()).hexdigest()[:12],
            "feature": "quiz", "source": "bank", "version": bank["version"], "model": bank["models"]["generator"],
            "topic": topic, "requested": n, "questions": questions, "item_status": ["verified"] * len(questions),
            "bank_item_ids": ids, "available": len(pool), "repeats": repeats, "topic_note": topic_note,
            "withheld": []}


def _spread(items: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Round-robin over topic groups; inside a group prefer the Bloom level used least so far."""
    by_group: dict[int, list[dict]] = {}
    for it in items:
        by_group.setdefault(it["group"], []).append(it)
    for group in by_group.values():
        rng.shuffle(group)
    order = list(by_group)
    rng.shuffle(order)
    chosen, bloom_used = [], {}
    while len(chosen) < n and any(by_group.values()):
        for g in order:
            if len(chosen) >= n or not by_group[g]:
                continue
            group = by_group[g]
            pick = min(group, key=lambda it: bloom_used.get(it["question"]["bloom_level"], 0))
            group.remove(pick)
            chosen.append(pick)
            level = pick["question"]["bloom_level"]
            bloom_used[level] = bloom_used.get(level, 0) + 1
    return chosen
