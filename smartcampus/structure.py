"""Step 3: slide types and sections, so every slide gets a place: "Lecture › Section › Slide n"."""
from __future__ import annotations

import re

from .text import word_count

ADMIN_TITLE = re.compile(r"\b(thank|questions\??$|q\s*&\s*a|references|bibliography|contact|acknowledg)", re.I)
AGENDA_TITLE = re.compile(r"\b(outline|agenda|contents|overview of (the )?lecture|today'?s topics)\b", re.I)
CONTINUED = re.compile(r"\s*[\(\[]?\s*(cont(inued|'d|\.)?|part\s*\d+|\d+\s*/\s*\d+)\s*[\)\]]?\s*$", re.I)
NON_CONTENT = {"title", "agenda", "section_divider", "administrative", "decorative_only"}


def heuristic_type(page: dict, first: bool) -> str:
    title, text = page.get("title", ""), page.get("text", "")
    words = word_count(text)
    if first and words <= 40:
        return "title"
    if AGENDA_TITLE.search(title):
        return "agenda"
    if ADMIN_TITLE.search(title) or (words <= 6 and ADMIN_TITLE.search(text)):
        return "administrative"
    if words <= 12 and not page.get("tables") and not page.get("has_images") and page.get("n_drawings", 0) < 5:
        return "section_divider"
    return "content"


def base_title(title: str) -> str:
    return CONTINUED.sub("", title or "").strip(" :-–").casefold()


def annotate(document: dict) -> dict:
    """Set page['slide_type'] and page['section'] in place (descriptions, when present, win)."""
    pages = document["pages"]
    for i, page in enumerate(pages):
        described = (page.get("description") or {}).get("slide_type")
        page["slide_type"] = described or heuristic_type(page, i == 0)

    dividers = [p for p in pages if p["slide_type"] == "section_divider"]
    current = document.get("name", "Lecture").rsplit(".", 1)[0]
    if len(dividers) >= 2:
        for page in pages:
            if page["slide_type"] == "section_divider":
                current = (page.get("title") or page.get("text", "")).strip()[:120] or current
            page["section"] = current
    else:
        previous = None
        for page in pages:
            key = base_title(page.get("title", ""))
            if page["slide_type"] in NON_CONTENT and page["slide_type"] != "section_divider":
                page["section"] = current if previous else "Introduction"
                continue
            if key and key != previous:
                current, previous = (page.get("title") or "").strip()[:120], key
            page["section"] = current
    return document


def content_pages(document: dict) -> list[dict]:
    return [p for p in document["pages"] if p.get("slide_type", "content") not in NON_CONTENT]


def topic_groups(pages: list[dict], n_groups: int) -> list[list[dict]]:
    """Split content pages, in lecture order, into n groups of similar size along section borders."""
    if not pages:
        return []
    sections: list[list[dict]] = []
    for page in pages:
        if sections and sections[-1][-1].get("section") == page.get("section"):
            sections[-1].append(page)
        else:
            sections.append([page])
    n_groups = max(1, min(n_groups, len(pages)))
    while len(sections) > n_groups:  # merge the smallest neighbouring pair
        sizes = [len(sections[i]) + len(sections[i + 1]) for i in range(len(sections) - 1)]
        i = sizes.index(min(sizes))
        sections[i:i + 2] = [sections[i] + sections[i + 1]]
    while len(sections) < n_groups:  # split the largest section
        i = max(range(len(sections)), key=lambda k: len(sections[k]))
        if len(sections[i]) < 2:
            break
        half = len(sections[i]) // 2
        sections[i:i + 1] = [sections[i][:half], sections[i][half:]]
    return sections
