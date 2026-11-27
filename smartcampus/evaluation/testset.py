"""Test-set format for the benchmark (see data/testsets/README.md)."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import DATA_DIR

TYPES = {"keyword", "paraphrase", "visual", "table", "math", "followup", "multi", "out_of_scope", "other"}
MODES = {"direct", "hint", "socratic", "practice"}


def load_testset(path: str | Path) -> dict:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = validate_testset(data)
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems[:10]))
    lecture = Path(data["lecture_file"])
    if not lecture.is_absolute():
        lecture = DATA_DIR / "lectures" / lecture
    data["_lecture_path"] = str(lecture)
    data["_path"] = str(path)
    return data


def validate_testset(data: dict) -> list[str]:
    problems = []
    for key in ("lecture_file", "questions"):
        if key not in data:
            problems.append(f"missing '{key}'")
    ids = set()
    for i, q in enumerate(data.get("questions", []), 1):
        where = f"question {q.get('id', i)}"
        if not q.get("id") or q["id"] in ids:
            problems.append(f"{where}: needs a unique id")
        ids.add(q.get("id"))
        if not str(q.get("question", "")).strip():
            problems.append(f"{where}: empty question")
        if q.get("type", "other") not in TYPES:
            problems.append(f"{where}: type must be one of {sorted(TYPES)}")
        if q.get("mode", "direct") not in MODES:
            problems.append(f"{where}: mode must be one of {sorted(MODES)}")
        gold = q.get("gold_pages", [])
        if not isinstance(gold, list) or not all(isinstance(p, int) and p > 0 for p in gold):
            problems.append(f"{where}: gold_pages must be a list of page numbers (1 = first page)")
        if q.get("type") != "out_of_scope" and not gold:
            problems.append(f"{where}: gold_pages is empty (only allowed for out_of_scope)")
        for turn in q.get("history", []):
            if turn.get("role") not in ("student", "tutor") or not turn.get("content"):
                problems.append(f"{where}: history turns need role 'student'/'tutor' and content")
                break
    return problems
