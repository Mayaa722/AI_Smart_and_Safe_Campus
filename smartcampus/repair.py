"""Step 13: repairs R0-R3. One attempt per item, generator model, feature system prompt.
Every repair returns (repaired output or None, log)."""
from __future__ import annotations

import json

from . import prompts, schemas


def _blocks(original: dict, findings: list[dict], source: str, extra: list[str] | None = None) -> list[str]:
    numbered = [{"id": f.get("id", f"f{i}"), **{k: v for k, v in f.items() if k != "id"}}
                for i, f in enumerate(findings, 1)]
    return ([f"<original_output>{json.dumps(original, ensure_ascii=False)}</original_output>",
             f'<findings source="{source}">\n{json.dumps(numbered, ensure_ascii=False)}\n</findings>']
            + (extra or []))


def _run(llm, base_user: str, blocks: list[str], task: str, schema: dict, images=None, name: str = "repair"):
    user = base_user + "\n\n" + "\n".join(blocks) + "\n\n" + task
    response = llm.generate(prompts.get("system"), user, schema=schema, images=images, task=name)
    log = {"task": name, "ok": response.ok, "error": response.error, "seconds": response.seconds,
           "usage": response.usage}
    if not response.ok:
        return None, log
    log["repair_notes"] = response.parsed.get("repair_notes", [])
    return response.parsed, log


def format_repair(llm, base_user: str, original: dict, findings: list[dict], schema: dict, images=None):
    """R0: fix code-check failures. base_user = the data blocks the original generation received."""
    out, log = _run(llm, base_user, _blocks(original, findings, "code"), prompts.get("R0"),
                    schemas.with_repair_notes(schema), images, "R0")
    return ({k: v for k, v in out.items() if k != "repair_notes"} if out else None), log


def repair_explanation(llm, base_user: str, original: dict, findings: list[dict], mode: str, version: str, images=None):
    task = prompts.render(prompts.get("R1"), mode=mode)
    out, log = _run(llm, base_user, _blocks(original, findings, "C1"), task,
                    schemas.with_repair_notes(schemas.explanation(version)), images, "R1")
    return ({k: v for k, v in out.items() if k != "repair_notes"} if out else None), log


def repair_summary(llm, base_user: str, original: dict, findings: list[dict], version: str):
    out, log = _run(llm, base_user, _blocks(original, findings, "C1"), prompts.get("R2"),
                    schemas.with_repair_notes(schemas.summary(version)), None, "R2")
    return ({k: v for k, v in out.items() if k != "repair_notes"} if out else None), log


def replacement_question(llm, base_user: str, withheld: dict, reasons: list[str], other_stems: list[str],
                         version: str):
    """R4: a NEW question in place of a withheld one (the withheld one is not repaired again)."""
    blocks = [f"<withheld_question>{json.dumps(withheld, ensure_ascii=False)}</withheld_question>",
              f"<withheld_reasons>{json.dumps(reasons, ensure_ascii=False)}</withheld_reasons>",
              f"<other_stems>{json.dumps(other_stems, ensure_ascii=False)}</other_stems>"]
    out, log = _run(llm, base_user, blocks, prompts.get("R4"), schemas.quiz_item_repair(version), None, "R4")
    if out is None:
        return None, log
    removed = any(n["action"] == "removed" for n in out.get("repair_notes", []))
    return ({"question": out["question"], "blueprint_item": out.get("blueprint_item"), "removed": removed}), log


def repair_question(llm, base_user: str, item: dict, findings: list[dict], item_results: dict,
                    other_stems: list[str], version: str):
    extra = [f"<item_results>{json.dumps(item_results, ensure_ascii=False)}</item_results>",
             f"<other_stems>{json.dumps(other_stems, ensure_ascii=False)}</other_stems>"]
    out, log = _run(llm, base_user, _blocks(item, findings, "C3+C4", extra), prompts.get("R3"),
                    schemas.quiz_item_repair(version), None, "R3")
    if out is None:
        return None, log
    removed = any(n["action"] == "removed" for n in out.get("repair_notes", []))
    return ({"question": out["question"], "blueprint_item": out.get("blueprint_item"), "removed": removed}), log
