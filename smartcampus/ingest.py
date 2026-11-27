"""Step 1: PDF / PPTX / DOCX -> one record per page: text, title, tables (Markdown), notes, image.

PPTX/DOCX are rendered to PDF for page images with LibreOffice (any OS) or, on Windows, with
installed PowerPoint/Word. Without a converter they are still ingested as text (no images).
For PPTX, text, tables and notes always come from python-pptx, which is more faithful than PDF text.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pymupdf as fitz

from .config import ROOT, RUNS_DIR

# If the optional pymupdf_layout package is installed (it comes with pymupdf4llm), PyMuPDF silently
# routes find_tables() through its ONNX layout model, which crashed the process natively (access
# violation) on a slide of the IoT test lecture. Use the standard table finder, so ingestion is
# stable and gives the same tables on every machine whether or not that package is present.
if hasattr(fitz, "_layout_enabled"):
    fitz._layout_enabled = False
from .text import looks_garbled

MAX_BYTES = 100 * 1024 * 1024
MAX_PAGES = 200
SUPPORTED = {".pdf", ".pptx", ".docx"}


def lecture_dir(doc_id: str) -> Path:
    return RUNS_DIR / "lectures" / doc_id


def load_document(doc_id: str) -> dict:
    return json.loads((lecture_dir(doc_id) / "document.json").read_text(encoding="utf-8"))


def ingest(path: str | Path, settings: dict, name: str | None = None) -> dict:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"Unsupported file type {suffix}; use PDF, PPTX or DOCX.")
    data = path.read_bytes()
    if not data or len(data) > MAX_BYTES:
        raise ValueError("The file is empty or larger than 100 MB.")
    sha = hashlib.sha256(data).hexdigest()
    doc_id = sha[:12]
    folder = lecture_dir(doc_id)
    manifest = folder / "document.json"
    if manifest.exists():
        return json.loads(manifest.read_text(encoding="utf-8"))
    (folder / "pages").mkdir(parents=True, exist_ok=True)
    source = folder / f"source{suffix}"
    source.write_bytes(data)

    warnings: list[str] = []
    pdf = source if suffix == ".pdf" else _convert_to_pdf(source, folder, warnings)
    pages = _pdf_pages(pdf, doc_id, folder, settings) if pdf else []
    if suffix == ".pptx":
        pages = _merge_pptx(source, pages, doc_id)
    elif suffix == ".docx" and not pages:
        pages = _docx_pages(source, doc_id)
    if not pages:
        raise ValueError("No pages could be read from this file.")
    if len(pages) > MAX_PAGES:
        raise ValueError(f"Lectures are limited to {MAX_PAGES} pages/slides.")
    for page in pages:
        page["garbled"] = looks_garbled(page["text"])

    document = {"doc_id": doc_id, "sha256": sha, "name": name or path.name, "type": suffix[1:],
                "page_count": len(pages), "has_images": any(p["image"] for p in pages),
                "warnings": warnings, "pages": pages}
    manifest.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    return document


# ---------- PDF ----------
def _pdf_pages(pdf: Path, doc_id: str, folder: Path, settings: dict) -> list[dict]:
    pages = []
    with fitz.open(pdf) as doc:
        if doc.needs_pass:
            raise ValueError("The PDF is password-protected; upload an unlocked copy.")
        for number, page in enumerate(doc, 1):
            scale = min(settings["render_dpi"] / 72, settings["render_max_px"] / max(page.rect.width, page.rect.height))
            image = folder / "pages" / f"p{number:04}.png"
            page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(image)
            pages.append({
                "page": number, "source_id": f"{doc_id}:p{number}", "doc_id": doc_id,
                "title": _pdf_title(page), "text": page.get_text("text").strip(),
                "tables": _pdf_tables(page), "notes": "", "image": str(image),
                "has_images": bool(page.get_images(full=True)),
                "n_drawings": _count_drawings(page)})
    return pages


def _pdf_title(page) -> str:
    best_size, lines = 0.0, []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line.get("spans", [])).strip()
            if not text:
                continue
            size = max(span["size"] for span in line["spans"])
            if size > best_size + 0.5:
                best_size, lines = size, [text]
            elif abs(size - best_size) <= 0.5 and len(lines) < 3:
                lines.append(text)
    return " ".join(lines)[:160]


def _pdf_tables(page) -> list[str]:
    try:
        return [t.to_markdown().strip() for t in page.find_tables().tables if t.row_count >= 2]
    except Exception:  # table detection is best-effort
        return []


def _count_drawings(page) -> int:
    try:
        return len(page.get_drawings())
    except Exception:
        return 0


# ---------- Office conversion ----------
def _convert_to_pdf(source: Path, folder: Path, warnings: list[str]) -> Path | None:
    target = folder / "rendered.pdf"
    if target.exists():
        return target
    office = _find_soffice()
    try:
        if office:
            subprocess.run([office, "--headless", "--convert-to", "pdf", "--outdir", str(folder), str(source)],
                           check=True, capture_output=True, timeout=300)
            produced = folder / (source.stem + ".pdf")
            produced.replace(target)
            return target
        if os.name == "nt":
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(ROOT / "tools" / "render_office.ps1"), "-Source", str(source),
                            "-Destination", str(target)], check=True, capture_output=True, timeout=300)
            if target.exists():
                return target
    except (subprocess.SubprocessError, OSError) as exc:
        warnings.append(f"Could not render {source.suffix} pages to images ({type(exc).__name__}).")
        return None
    warnings.append("No LibreOffice or Microsoft Office found: pages were read as text only, without images. "
                    "Install LibreOffice or upload a PDF export to enable visual descriptions.")
    return None


def _find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for candidate in [r"C:\Program Files\LibreOffice\program\soffice.exe",
                      r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                      "/Applications/LibreOffice.app/Contents/MacOS/soffice"]:
        if Path(candidate).exists():
            return candidate
    return None


# ---------- PPTX ----------
def _merge_pptx(source: Path, rendered: list[dict], doc_id: str) -> list[dict]:
    from pptx import Presentation

    slides = Presentation(str(source)).slides
    pages = []
    for number, slide in enumerate(slides, 1):
        texts, tables, pictures = [], [], 0
        for shape in _walk_shapes(slide.shapes):
            if getattr(shape, "has_table", False) and shape.has_table:
                tables.append(_rows_to_markdown([[cell.text.strip() for cell in row.cells] for row in shape.table.rows]))
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
            if shape.shape_type == 13:  # MSO_SHAPE_TYPE.PICTURE
                pictures += 1
        title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title is not None else ""
        notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide else ""
        base = rendered[number - 1] if number - 1 < len(rendered) else {}
        pages.append({
            "page": number, "source_id": f"{doc_id}:p{number}", "doc_id": doc_id,
            "title": title or base.get("title", ""), "text": "\n".join(texts), "tables": tables,
            "notes": notes, "image": base.get("image"),
            "has_images": pictures > 0 or base.get("has_images", False),
            "n_drawings": base.get("n_drawings", 0)})
    return pages


def _walk_shapes(shapes):
    for shape in shapes:
        if shape.shape_type == 6:  # group
            yield from _walk_shapes(shape.shapes)
        else:
            yield shape


# ---------- DOCX (text-only fallback) ----------
def _docx_pages(source: Path, doc_id: str, max_chars: int = 1500) -> list[dict]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(str(source))
    pages, title, buffer, tables = [], "", [], []

    def flush():
        if buffer or tables:
            number = len(pages) + 1
            pages.append({"page": number, "source_id": f"{doc_id}:p{number}", "doc_id": doc_id,
                          "title": title, "text": "\n".join(buffer), "tables": list(tables), "notes": "",
                          "image": None, "has_images": False, "n_drawings": 0})
            buffer.clear()
            tables.clear()

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            if paragraph.style is not None and paragraph.style.name.lower().startswith(("heading", "title")):
                flush()
                title = text
            elif sum(len(t) for t in buffer) + len(text) > max_chars:
                flush()
            buffer.append(text)
        elif tag == "tbl":
            table = Table(child, document)
            tables.append(_rows_to_markdown([[cell.text.strip() for cell in row.cells] for row in table.rows]))
    flush()
    return pages


def _rows_to_markdown(rows: list[list[str]]) -> str:
    rows = [[cell.replace("|", "\\|").replace("\n", " ") for cell in row] for row in rows if any(row)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)
