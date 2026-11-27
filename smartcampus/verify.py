"""Steps 12 and 15: model-based checkers C1-C6 and the metrics computed from their output.
Checkers always run with the checker/judge model (a different family from the generator)."""
from __future__ import annotations

import json
import random
import re

from . import prompts, schemas
from .checks import inline_citations, quote_in_slide
from .evidence import evidence_block
from .llm import estimate_tokens

LETTERS = "ABCD"


def _user(lecture_name: str, slides: list[dict], blocks: list[str], task: str) -> str:
    return "\n\n".join([evidence_block(lecture_name, slides)] + blocks + [task])


def _system() -> str:
    return prompts.get("checker_system")


SAFETY = 0.75  # estimate_tokens() reads about 15% low, and Groq counts the reserved output in its per-minute limit


def input_budget(llm) -> int | None:
    """Largest input (estimated tokens) every model behind `llm` accepts in one request, or None if unlimited."""
    budgets = []
    for spec in (getattr(m, "spec", None) for m in (getattr(llm, "primary", llm), getattr(llm, "backup", None))):
        if not spec:
            continue
        limit = spec.get("max_input_tokens", 10**9)
        if "tpm" in spec:
            limit = min(limit, spec["tpm"] - spec.get("max_output_tokens", 4096))
        budgets.append(limit)
    return int(min(budgets) * SAFETY) if budgets and min(budgets) < 10**8 else None


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p for p in (x.strip() for x in parts) if p]


def _check_parts(lecture_name: str, slides: list[dict], candidate: str, kind: str, budget: int) -> list[tuple[list[dict], str]]:
    """Split a long candidate into consecutive sentence groups, each sent with only the slides it cites
    (uncited sentences borrow the slides of their neighbours), so every request fits `budget`."""
    by_id = {s["source_id"]: s for s in slides}
    sentences = _sentences(candidate)
    cited = [[c for c in inline_citations(s) if c in by_id] for s in sentences]
    for i, ids in enumerate(cited):  # uncited sentence: use the nearest cited neighbour's slides
        if not ids:
            for j in sorted(range(len(cited)), key=lambda j: abs(j - i)):
                if cited[j]:
                    cited[i] = cited[j]
                    break
    overhead = estimate_tokens(_system(), prompts.get("C1"), f'<candidate type="{kind}"></candidate>') + 200

    def cost(group: list[int]) -> int:
        ids = {c for i in group for c in cited[i]}
        return overhead + estimate_tokens(evidence_block(lecture_name, [by_id[c] for c in ids]),
                                          *(sentences[i] for i in group))

    groups, current = [], []
    for i in range(len(sentences)):
        if current and cost(current + [i]) > budget:
            groups.append(current)
            current = []
        current.append(i)
    if current:
        groups.append(current)
    parts = []
    for group in groups:
        ids = list(dict.fromkeys(c for i in group for c in cited[i]))
        parts.append(([by_id[c] for c in ids] or slides[:1], " ".join(sentences[i] for i in group)))
    return parts


def _claims_once(llm, lecture_name: str, slides: list[dict], candidate: str, kind: str) -> dict:
    by_id = {s["source_id"]: s for s in slides}
    block = f'<candidate type="{kind}">{candidate}</candidate>'
    result, retried = None, False
    for attempt in range(2):
        task = prompts.get("C1") + ("\nCopy every quote exactly, word for word, from the slide." if attempt else "")
        response = llm.generate(_system(), _user(lecture_name, slides, [block], task), schema=schemas.C1, task="C1")
        if not response.ok:
            return {"ok": False, "error": response.error}
        bad = [c for c in response.parsed["claims"]
               if c["supporting_ids"] and c["quote"]
               and not any(quote_in_slide(c["quote"], by_id[s], include_visual=True) for s in c["supporting_ids"] if s in by_id)]
        result = response.parsed
        if not bad:
            break
        retried = True
    return {"ok": True, "claims": result["claims"], "retried": retried, "model": response.model}


def _claims_piece(llm, lecture_name: str, slides: list[dict], candidate: str, kind: str, depth: int = 0) -> dict:
    """One piece; if the checker's reply is cut off (its hidden reasoning sometimes runs long),
    check the two halves separately, or retry a single sentence once."""
    part = _claims_once(llm, lecture_name, slides, candidate, kind)
    if part["ok"] or not str(part.get("error", "")).startswith("Incomplete output") or depth >= 2:
        return part
    sentences = _sentences(candidate)
    if len(sentences) < 2:
        return _claims_once(llm, lecture_name, slides, candidate, kind)
    half = len(sentences) // 2
    merged = {"ok": True, "claims": [], "retried": True, "model": None}
    for text in (" ".join(sentences[:half]), " ".join(sentences[half:])):
        piece = _claims_piece(llm, lecture_name, slides, text, kind, depth + 1)
        if not piece["ok"]:
            return piece
        merged["claims"] += piece["claims"]
        merged["model"] = piece["model"]
    return merged


# ---------- C1: claims -> faithfulness ----------
def claim_check(llm, lecture_name: str, slides: list[dict], candidate: str, kind: str) -> dict:
    by_id = {s["source_id"]: s for s in slides}
    budget = input_budget(llm)
    whole = estimate_tokens(_system(), _user(lecture_name, slides,
                                             [f'<candidate type="{kind}">{candidate}</candidate>'], prompts.get("C1")))
    if budget is None or whole <= budget:
        parts = [(slides, candidate)]
    else:  # too large for the checker's per-request limit: check it in pieces
        parts = _check_parts(lecture_name, slides, candidate, kind, budget)
    claims, retried, model = [], False, None
    for part_slides, part_text in parts:
        part = _claims_piece(llm, lecture_name, part_slides, part_text, kind)
        if not part["ok"]:
            return {"ok": False, "error": part["error"]}
        claims += part["claims"]
        retried = retried or part["retried"]
        model = part["model"]
    unverified = [c for c in claims if c["supporting_ids"] and c["quote"]
                  and not any(quote_in_slide(c["quote"], by_id[s], include_visual=True) for s in c["supporting_ids"] if s in by_id)]
    counted = [c for c in claims if c["label"] != "additional_teaching" and c not in unverified]
    count = {label: sum(c["label"] == label for c in counted)
             for label in ("supported", "partially_supported", "not_supported", "contradicted")}
    denominator = sum(count.values())
    cited = [c for c in counted if c["cited_ids"]]
    return {"ok": True, "model": model, "claims": claims, "counts": count, "retried": retried, "parts": len(parts),
            "n_claims": len(claims), "additional_teaching": sum(c["label"] == "additional_teaching" for c in claims),
            "unverified_quotes": len(unverified),
            "faithfulness": count["supported"] / denominator if denominator else None,
            "contradiction_rate": count["contradicted"] / denominator if denominator else None,
            "citation_accuracy": (sum(bool(set(c["cited_ids"]) & set(c["supporting_ids"])) for c in cited) / len(cited)
                                  if cited else None)}


def claim_findings(check: dict) -> list[dict]:
    findings = []
    for i, c in enumerate(check.get("claims", []), 1):
        wrong_citation = c["label"] == "supported" and c["cited_ids"] and not set(c["cited_ids"]) & set(c["supporting_ids"])
        if c["label"] in ("contradicted", "not_supported", "partially_supported") or wrong_citation:
            findings.append({"id": f"f{i}", "issue": "wrong citation" if wrong_citation else c["label"],
                             "details": f"Claim: {c['claim']} | Reason: {c['reason']}",
                             "slide_ids": c["supporting_ids"] or c["cited_ids"]})
    return findings


def claims_flagged(check: dict, threshold: float) -> bool:
    if not check.get("ok"):
        return False
    return check["counts"]["contradicted"] > 0 or (check["faithfulness"] is not None and check["faithfulness"] < threshold)


# ---------- C2: key-point coverage ----------
def coverage_check(llm, lecture_name: str, slides: list[dict], key_points: list[dict], candidate: str, kind: str) -> dict:
    blocks = ["<key_points>\n" + json.dumps([{k: p[k] for k in ("id", "point", "slide_ids")} for p in key_points],
                                            ensure_ascii=False) + "\n</key_points>",
              f'<candidate type="{kind}">{candidate}</candidate>']
    response = llm.generate(_system(), _user(lecture_name, slides, blocks, prompts.get("C2")), schema=schemas.C2, task="C2")
    if not response.ok:
        return {"ok": False, "error": response.error}
    points = response.parsed["points"]
    from .text import squash
    for p in points:  # quote must come from the candidate
        if p["label"] != "missing" and p["candidate_quote"] and squash(p["candidate_quote"]) not in squash(candidate):
            p["quote_unverified"] = True
    labels = [p["label"] for p in points]
    total = len(key_points)
    return {"ok": True, "points": points,
            "coverage": (labels.count("covered") + 0.5 * labels.count("partial")) / total if total else None,
            "incorrect_rate": labels.count("incorrect") / total if total else None}


# ---------- C3: blind solver ----------
def blind_solve(llm, lecture_name: str, slides: list[dict], question: dict, seed: int = 0) -> dict:
    order = list(range(4))
    random.Random(seed).shuffle(order)
    item = {"stem": question["stem"], "options": {LETTERS[i]: question["options"][j] for i, j in enumerate(order)}}
    block = "<quiz_item>\n" + json.dumps(item, ensure_ascii=False) + "\n</quiz_item>"
    response = llm.generate(_system(), _user(lecture_name, slides, [block], prompts.get("C3")), schema=schemas.C3, task="C3")
    if not response.ok:
        return {"ok": False, "error": response.error, "model": response.model}
    out = response.parsed
    to_original = lambda letter: None if letter == "none" else order[LETTERS.index(letter)]
    best = to_original(out["best_option"])
    return {"ok": True, "model": response.model, "raw": out, "verdict": out["verdict"],
            "best_index": best, "defensible_indices": sorted(to_original(x) for x in out["defensible_options"]),
            "key_confirmed": out["verdict"] == "single_answer" and best == question["answer_index"]}


# ---------- C4: item review ----------
def review_item(llm, lecture_name: str, slides: list[dict], question: dict) -> dict:
    item = {"stem": question["stem"], "options": {LETTERS[i]: o for i, o in enumerate(question["options"])},
            "key": LETTERS[question["answer_index"]], "explanation": question["explanation"],
            "distractor_rationales": question["distractor_rationales"], "bloom_level": question["bloom_level"],
            "citations": question["citations"]}
    block = "<quiz_item>\n" + json.dumps(item, ensure_ascii=False) + "\n</quiz_item>"
    response = llm.generate(_system(), _user(lecture_name, slides, [block], prompts.get("C4")), schema=schemas.C4, task="C4")
    if not response.ok:
        return {"ok": False, "error": response.error}
    return {"ok": True, "model": response.model, **response.parsed}


def quiz_item_valid(solves: list[dict], review: dict) -> bool:
    return (len(solves) >= 1 and all(s.get("ok") and s["key_confirmed"] for s in solves)
            and review.get("ok") and review["verdict"] == "pass")


# ---------- C5: tutor mode ----------
def mode_check(llm, lecture_name: str, slides: list[dict], history: str, turn: dict) -> dict:
    block = (f"<conversation_history>{history or '(none)'}</conversation_history>\n"
             f'<turn mode="{turn["mode"]}" covered_by_lecture="{turn.get("covered_by_lecture", "")}">\n'
             f"<student_request>{turn['message']}</student_request>\n<tutor_reply>{turn['answer']}</tutor_reply>\n</turn>")
    response = llm.generate(_system(), _user(lecture_name, slides, [block], prompts.get("C5")), schema=schemas.C5, task="C5")
    if not response.ok:
        return {"ok": False, "error": response.error}
    checks = response.parsed["checks"]
    applicable = [c for c in checks.values() if c["value"] != "not_applicable"]
    return {"ok": True, "checks": checks,
            "compliance": sum(c["value"] == "true" for c in applicable) / len(applicable) if applicable else None}


# ---------- C6: key-point builder (draft for human review) ----------
def build_key_points(llm, lecture_name: str, slides: list[dict], question: str | None = None) -> dict:
    task = prompts.render(prompts.get("C6"), question=question or "")
    response = llm.generate(_system(), _user(lecture_name, slides, [], task), schema=schemas.C6, task="C6")
    if not response.ok:
        return {"ok": False, "error": response.error}
    allowed = {s["source_id"] for s in slides}
    points = [p for p in response.parsed["key_points"] if set(p["slide_ids"]) <= allowed and p["slide_ids"]]
    return {"ok": True, "key_points": points, "status": "draft - needs human review"}
