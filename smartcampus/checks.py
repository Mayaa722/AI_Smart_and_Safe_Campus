"""Step 12, part 1: checks done by code (no model). Each check returns a list of findings;
an empty list means the output passed. Findings are plain dicts, ready for R0 format repair."""
from __future__ import annotations

import re

from .text import squash, word_count

SLIDE_ID = re.compile(r"[0-9a-f]{12}:p\d+")
# one id per bracket, as the prompts ask, or several in one bracket ("[id1, id2]" / "[id1; id2]"), as models often write
CITATION = re.compile(r"\[\s*([0-9a-f]{12}:p\d+(?:\s*[,;]\s*[0-9a-f]{12}:p\d+)*)\s*\]")
BANNED_OPTIONS = re.compile(r"\b(all|none) of the above\b|\bboth [a-d] and [a-d]\b", re.I)
NEGATIVE_STEM = re.compile(r"\b(NOT|EXCEPT)\b")


def finding(issue: str, details: str = "", slide_ids: list[str] | None = None) -> dict:
    return {"issue": issue, "details": details, "slide_ids": slide_ids or []}


def inline_citations(text: str) -> list[str]:
    return [i for group in CITATION.findall(text or "") for i in SLIDE_ID.findall(group)]


def citation_findings(output: dict, allowed: set[str], text_fields: tuple[str, ...]) -> list[dict]:
    cited = set(output.get("citations", []))
    for name in text_fields:
        cited.update(inline_citations(output.get(name, "")))
    unknown = sorted(cited - allowed)
    return [finding("Unknown slide ids cited", ", ".join(unknown))] if unknown else []


def quote_in_slide(quote: str, slide: dict, include_visual: bool = False) -> bool:
    quote = squash(quote)
    if not quote:
        return False
    if quote.startswith("image:"):
        return True
    haystack = [slide.get("text", ""), *slide.get("tables", [])]
    if include_visual:
        description = slide.get("description") or {}
        haystack += [description.get("description", ""), description.get("transcription", "")]
        haystack += [t.get("markdown", "") for t in description.get("tables", [])]
    return any(quote in squash(h) for h in haystack)


# ---------- explanation ----------
WORD_LIMITS = {"direct": 300, "hint": 80, "socratic": 100}


def explanation_findings(output: dict, allowed: set[str], slides: dict[str, dict], mode: str,
                         n_questions: int = 1, version: str = "A") -> list[dict]:
    found = citation_findings(output, allowed, ("answer",))
    limit = WORD_LIMITS.get(mode)
    if mode == "direct" and n_questions > 1:
        limit = min(600, 300 * n_questions)
    words = word_count(output.get("answer", ""))
    if limit and words > int(limit * 1.15):  # 15% tolerance before asking for a rewrite
        found.append(finding("Answer too long", f"{words} words; limit for {mode} mode is {limit}."))
    if version == "B":
        noted = {n["slide_id"] for n in output.get("evidence_notes", [])}
        for note in output.get("evidence_notes", []):
            slide = slides.get(note["slide_id"])
            if slide is None or not quote_in_slide(note["quote"], slide):
                found.append(finding("Quote not found word for word in the slide",
                                     note["quote"][:200], [note["slide_id"]]))
        missing = sorted(set(inline_citations(output.get("answer", ""))) - noted)
        if missing:
            found.append(finding("Cited slides missing from evidence_notes", ", ".join(missing), missing))
    return found


# ---------- summary ----------
def coverage_findings(body: str, content_ids: list[str], parts: int = 4) -> list[dict]:
    """A summary must cite slides from every part of the lecture (2026-11-26: the big data summary
    stopped after slide 22 of 86). The content slides are split into `parts` equal runs, in order."""
    if len(content_ids) < 2 * parts:
        return []
    cited = set(inline_citations(body))
    found = []
    for k in range(parts):
        run = content_ids[k * len(content_ids) // parts:(k + 1) * len(content_ids) // parts]
        if not cited & set(run):
            first, last = run[0].split(":")[1], run[-1].split(":")[1]
            found.append(finding("Part of the lecture not covered",
                                 f"No bullet cites slides {first}-{last} (part {k + 1} of {parts} of the lecture). "
                                 "Add the major topics of these slides in lecture order, citing them.", run))
    return found


def summary_findings(output: dict, allowed: set[str], content_ids: list[str], version: str = "A") -> list[dict]:
    found = citation_findings(output, allowed, ("body",))
    words = word_count(output.get("body", ""))
    if words > 800:
        found.append(finding("Summary too long", f"{words} words; the maximum is 800."))
    if words < 150:
        found.append(finding("Summary too short", f"{words} words; aim for 350-650."))
    for heading in ("## Overview", "## Key takeaways"):
        if heading.lower() not in output.get("body", "").lower():
            found.append(finding("Missing required section", heading))
    found += coverage_findings(output.get("body", ""), content_ids)
    if version == "B":
        placed = [sid for item in output.get("outline", []) for sid in item["slide_ids"]]
        duplicates = sorted({sid for sid in placed if placed.count(sid) > 1})
        missing = sorted(set(content_ids) - set(placed) - set(output.get("skipped_slides", [])))
        if duplicates:
            found.append(finding("Slides placed in more than one outline topic", ", ".join(duplicates), duplicates))
        if missing:
            found.append(finding("Content slides missing from the outline", ", ".join(missing), missing))
    return found


# ---------- quiz ----------
def question_findings(q: dict, allowed: set[str]) -> list[str]:
    issues = []
    options = q.get("options", [])
    if len(options) != 4:
        issues.append("Needs exactly 4 options")
    if len({squash(o) for o in options}) != len(options):
        issues.append("Duplicate options")
    if not 0 <= q.get("answer_index", -1) < len(options):
        issues.append("answer_index out of range")
    if len(q.get("distractor_rationales", [])) != max(0, len(options) - 1):
        issues.append("Needs one distractor rationale per wrong option")
    if not q.get("citations"):
        issues.append("No citations")
    if set(q.get("citations", [])) - allowed:
        issues.append("Unknown citation ids")
    text = q.get("stem", "") + " " + " ".join(options)
    if CITATION.search(text) or re.search(r"[0-9a-f]{12}:p\d+", text):
        issues.append("Slide ids inside the stem or options")
    if any(BANNED_OPTIONS.search(o) for o in options):
        issues.append("Banned option phrase (all/none of the above, both A and B)")
    if NEGATIVE_STEM.search(q.get("stem", "")):
        issues.append("Negatively worded stem (NOT/EXCEPT)")
    if len(options) == 4 and 0 <= q.get("answer_index", -1) < 4:
        key_len = len(options[q["answer_index"]])
        others = [len(o) for i, o in enumerate(options) if i != q["answer_index"]]
        if others and key_len > 1.8 * (sum(others) / len(others)) and key_len - max(others) > 20:
            issues.append("Correct option much longer than the others (length cue)")
    return issues


def quiz_findings(output: dict, allowed: set[str], slides: dict[str, dict], version: str = "A") -> list[dict]:
    found = []
    stems = [squash(q["stem"]) for q in output.get("questions", [])]
    for i, q in enumerate(output.get("questions", []), 1):
        for issue in question_findings(q, allowed):
            found.append(finding(f"Question {i}: {issue}", q["stem"][:160], q.get("citations", [])))
        if stems.count(squash(q["stem"])) > 1:
            found.append(finding(f"Question {i}: duplicate stem", q["stem"][:160]))
    if version == "B":
        for i, item in enumerate(output.get("blueprint", []), 1):
            ok = any(quote_in_slide(item["key_fact_quote"], slides[sid])
                     for sid in item["slide_ids"] if sid in slides)
            if not ok:
                found.append(finding(f"Blueprint item {i}: key_fact_quote not found word for word",
                                     item["key_fact_quote"][:200], item["slide_ids"]))
    return found


# ---------- slide descriptions ----------
def description_findings(output: dict, expected_id: str) -> list[dict]:
    found = []
    if output.get("slide_id") != expected_id:
        found.append(finding("slide_id must equal the requested id", f"expected {expected_id}"))
    words = word_count(output.get("description", ""))
    if output.get("slide_type") == "content" and not 40 <= words <= 165:
        found.append(finding("Description length outside 40-150 words", f"{words} words"))
    text = output.get("transcription", "") + output.get("description", "")
    if len(re.findall(r"(?<!\\)\$", text)) % 2:
        found.append(finding("Unbalanced $ math delimiters"))
    for table in output.get("tables", []):
        rows = [r for r in table["markdown"].splitlines() if r.strip().startswith("|")]
        widths = {r.strip().strip("|").count("|") for r in rows}
        if len(widths) > 1:
            found.append(finding("Table rows have different numbers of columns", table["caption"]))
    terms = output.get("search_terms", [])
    if output.get("slide_type") == "content" and not 3 <= len(terms) <= 10:
        found.append(finding("search_terms must have 3-10 entries", str(len(terms))))
    if any(word_count(t) > 6 for t in terms):
        found.append(finding("A search term is longer than 6 words"))
    return found
