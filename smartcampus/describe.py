"""Step 2: a vision model describes slides whose content the text layer misses.

Descriptions are written once per (image, prompt version, model), cached, and shared by every
generator model so the benchmark compares models on identical evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import prompts, schemas
from .checks import description_findings
from .evidence import attr
from .ingest import lecture_dir
from .structure import annotate


UNDESCRIBED_WARNING = "No AI description yet for"


def needs_description(page: dict) -> bool:
    if not page.get("image"):
        return False
    return (page.get("has_images") or page.get("n_drawings", 0) >= 15
            or len(page.get("text", "").strip()) < 200 or page.get("garbled", False))


def _header(document: dict, page: dict) -> str:
    pages = document["pages"]
    i = page["page"] - 1
    previous = pages[i - 1].get("title", "") if i > 0 else ""
    following = pages[i + 1].get("title", "") if i + 1 < len(pages) else ""
    return (f'<slide id="{page["source_id"]}" page="{page["page"]}" lecture="{attr(document["name"])}" '
            f'section="{attr(page.get("section") or "unknown")}">\n'
            f'<neighbour_titles previous="{attr(previous)}" next="{attr(following)}"/>\n'
            f'<extracted_text>{page.get("text") or "(none)"}</extracted_text>\n</slide>\n[slide image attached]')


def describe_page(llm, document: dict, page: dict) -> tuple[dict | None, dict]:
    """Return (description or None, log record)."""
    system = prompts.get("describe_system")
    user = _header(document, page) + "\n\n" + prompts.get("describe_task")
    response = llm.generate(system, user, schema=schemas.DESCRIPTION,
                            images=[(page["source_id"], page["image"])], task="describe")
    log = {"source_id": page["source_id"], "first": _log(response), "repair": None, "findings": []}
    if not response.ok:
        return None, log
    output, found = response.parsed, description_findings(response.parsed, page["source_id"])
    if found:
        repair_user = (_header(document, page) + "\n\n<original_output>" + json.dumps(output, ensure_ascii=False)
                       + "</original_output>\n<findings source=\"code\">" + json.dumps(found, ensure_ascii=False)
                       + "</findings>\n\n" + prompts.get("R0"))
        repaired = llm.generate(system, repair_user, schema=schemas.with_repair_notes(schemas.DESCRIPTION),
                                images=[(page["source_id"], page["image"])], task="describe_repair")
        log["repair"] = _log(repaired)
        if repaired.ok:
            candidate = {k: v for k, v in repaired.parsed.items() if k != "repair_notes"}
            if len(description_findings(candidate, page["source_id"])) <= len(found):
                output = candidate
        found = description_findings(output, page["source_id"])
    log["findings"] = found
    if output.get("slide_id") != page["source_id"]:
        return None, log  # never attach a description to the wrong slide
    return output, log


def describe_document(document: dict, llm, progress=None) -> dict:
    """Describe every slide that needs it; reuse cached results; save into the document."""
    folder = lecture_dir(document["doc_id"])
    cache_path = folder / "descriptions.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    version = prompts.prompt_hash("describe_system", "describe_task")
    annotate(document)  # provisional sections for the header
    todo = [p for p in document["pages"] if needs_description(p)]
    logs = []
    for n, page in enumerate(todo, 1):
        image_sha = hashlib.sha256(Path(page["image"]).read_bytes()).hexdigest()
        key = f"{page['source_id']}|{image_sha}|{version}|{llm.spec['model']}"
        if key not in cache:
            if progress:
                progress(f"Describing slide {page['page']} ({n}/{len(todo)})")
            description, log = describe_page(llm, document, page)
            logs.append(log)
            if description is None:
                continue
            cache[key] = description
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        page["description"] = cache[key]
    # Never report a lecture as complete while slides that need a description have none
    # (e.g. the provider was overloaded). They stay out of the cache, so the next run retries only them.
    missing = [p["source_id"] for p in todo if not p.get("description")]
    document["undescribed"] = missing
    document["warnings"] = [w for w in document.get("warnings", []) if not w.startswith(UNDESCRIBED_WARNING)]
    if missing:
        document["warnings"].append(f"{UNDESCRIBED_WARNING} {len(missing)} of {len(todo)} slides that need one "
                                    f"(pages {', '.join(m.split(':p')[1] for m in missing)}). Process the lecture "
                                    "again later to retry only these.")
    annotate(document)  # final slide types and sections
    (folder / "document.json").write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    if logs:
        with (folder / "describe_log.jsonl").open("a", encoding="utf-8") as stream:
            for log in logs:
                stream.write(json.dumps(log, ensure_ascii=False) + "\n")
    return {"described": len(todo) - len(missing), "needed": len(todo), "failed": missing}


def _log(response) -> dict:
    return {"ok": response.ok, "error": response.error, "seconds": response.seconds,
            "usage": response.usage, "cached": response.cached, "model": response.model}
