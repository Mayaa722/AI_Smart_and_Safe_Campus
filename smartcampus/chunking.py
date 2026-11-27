"""Steps 4-5: context headers and chunks.

Modes (settings.retrieval.chunking):
- "small_to_big": search small child chunks (512/1000 chars), answer with the whole parent slide.
- "slide":        one chunk per slide.
- "flat":         the paper's baseline: fixed-size text chunks, returned as chunks (top-K counts chunks).
Tables are never split mid-row: big tables are split into row groups that repeat the header row.
"""
from __future__ import annotations


def header(document: dict, page: dict) -> str:
    title = page.get("title") or ""
    return f"{document['name'].rsplit('.', 1)[0]} › {page.get('section', '')} › slide {page['page']}" + (f": {title}" if title else "")


def windows(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    result, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n", start + size // 2, end), text.rfind(" ", start + size // 2, end))
            if cut > start:
                end = cut
        result.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [w for w in result if w]


def table_parts(markdown: str, size: int) -> list[str]:
    lines = [l for l in markdown.strip().splitlines() if l.strip()]
    if len(markdown) <= size or len(lines) <= 3:
        return [markdown.strip()]
    head, rows = lines[:2], lines[2:]
    parts, current = [], []
    for row in rows:
        if current and len("\n".join(head + current + [row])) > size:
            parts.append("\n".join(head + current))
            current = []
        current.append(row)
    if current:
        parts.append("\n".join(head + current))
    return parts


def table_sentence(page: dict, markdown: str, caption: str = "") -> str:
    first = markdown.strip().splitlines()[0] if markdown.strip() else ""
    columns = [c.strip() for c in first.strip().strip("|").split("|") if c.strip()]
    label = caption or page.get("title") or f"slide {page['page']}"
    return f"Table ({label}) with columns: {', '.join(columns)}."


def description_text(page: dict) -> str:
    d = page.get("description") or {}
    parts = [d.get("description", ""), d.get("transcription", "")]
    if d.get("search_terms"):
        parts.append("Search terms: " + ", ".join(d["search_terms"]))
    return "\n".join(p for p in parts if p)


def build_chunks(document: dict, settings: dict) -> list[dict]:
    r = settings["retrieval"]
    size = settings["chunk_chars"][settings.get("subject_profile", "stem")]
    overlap = settings.get("chunk_overlap", 64)
    mode = r.get("chunking", "small_to_big")
    use_desc, use_header = r.get("use_descriptions", True), r.get("use_context_header", True)
    chunks = []

    def add(page, kind, embed_text, bm25_text=None):
        prefix = header(document, page) + "\n" if use_header else ""
        chunks.append({"chunk_id": f"{page['source_id']}:c{len([c for c in chunks if c['source_id'] == page['source_id']]) + 1}",
                       "source_id": page["source_id"], "page": page["page"], "kind": kind,
                       "slide_type": page.get("slide_type", "content"), "section": page.get("section", ""),
                       "embed_text": prefix + embed_text, "bm25_text": prefix + (bm25_text or embed_text)})

    for page in document["pages"]:
        body = "\n".join(t for t in [page.get("text", ""), page.get("notes", "")] if t)
        visual = description_text(page) if use_desc else ""
        tables = [(t, "") for t in page.get("tables", [])]
        if use_desc:
            tables += [(t["markdown"], t["caption"]) for t in (page.get("description") or {}).get("tables", [])]
        if mode == "slide":
            add(page, "slide", "\n".join(x for x in [body, visual] + [t for t, _ in tables] if x))
            continue
        for window in windows(body, size, overlap):
            add(page, "text", window)
        if visual:
            for window in windows(visual, size, overlap):
                add(page, "visual", window)
        for markdown, caption in tables:
            for part in table_parts(markdown, size * 2):
                add(page, "table", table_sentence(page, part, caption), part)
        if not any(c["source_id"] == page["source_id"] for c in chunks) and page.get("title"):
            add(page, "text", page["title"])
    return chunks
