"""Load prompt text straight from prompts/*.md so the Markdown files stay the single source of truth.

Each prompt is the first fenced block (not ```json) under a given heading whose text starts with
a given prefix. Task prompts are returned from "TASK:" onward: the data blocks that precede the
task in the documentation are built by code (see evidence.py) in the documented format.
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache

from .config import PROMPTS_DIR

FEATURES = "SmartCampus_prompts_v1.md"
CHECKING = "SmartCampus_checking_prompts_v1.md"
REPAIR = "SmartCampus_repair_prompts_v1.md"
DESCRIBE = "SmartCampus_description_prompt_v1.md"
QUERY = "SmartCampus_query_prompt_v1.md"

# name -> (file, heading prefix, block must contain this text)
REGISTRY = {
    "system": (FEATURES, "## 1.", "You are SmartCampus Tutor"),
    "explain_A": (FEATURES, "### 3A.", "TASK:"),
    "explain_B": (FEATURES, "### 3B.", "TASK:"),
    "summary_A": (FEATURES, "### 4A.", "TASK:"),
    "summary_B": (FEATURES, "### 4B.", "TASK:"),
    "quiz_A": (FEATURES, "### 5A.", "TASK:"),
    "quiz_B": (FEATURES, "### 5B.", "TASK:"),
    "checker_system": (CHECKING, "## 0.", "You are a strict"),
    "C1": (CHECKING, "## C1.", "TASK:"),
    "C2": (CHECKING, "## C2.", "TASK:"),
    "C3": (CHECKING, "## C3.", "TASK:"),
    "C4": (CHECKING, "## C4.", "TASK:"),
    "C5": (CHECKING, "## C5.", "TASK:"),
    "C6": (CHECKING, "## C6.", "TASK:"),
    "R0": (REPAIR, "## R0.", "TASK:"),
    "R1": (REPAIR, "## R1.", "TASK:"),
    "R2": (REPAIR, "## R2.", "TASK:"),
    "R3": (REPAIR, "## R3.", "TASK:"),
    "R4": (REPAIR, "## R4.", "TASK:"),
    "describe_system": (DESCRIBE, "## System prompt", "You are"),
    "describe_task": (DESCRIBE, "## Task prompt", "TASK:"),
    "query_system": (QUERY, "## System prompt", "You prepare"),
    "query_task": (QUERY, "## Task prompt", "TASK:"),
}

FENCE = re.compile(r"^```(\w*)\s*$")


def _section(lines: list[str], heading_prefix: str) -> list[str]:
    level = heading_prefix.split(" ")[0]
    start = next((i for i, line in enumerate(lines) if line.startswith(heading_prefix)), None)
    if start is None:
        raise KeyError(f"Heading not found: {heading_prefix}")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        hashes = re.match(r"^(#+) ", lines[i])
        if hashes and len(hashes.group(1)) <= len(level):
            end = i
            break
    return lines[start:end]


def _blocks(lines: list[str]) -> list[tuple[str, str]]:
    blocks, inside, lang, current = [], False, "", []
    for line in lines:
        match = FENCE.match(line)
        if match and not inside:
            inside, lang, current = True, match.group(1), []
        elif line.strip() == "```" and inside:
            blocks.append((lang, "\n".join(current)))
            inside = False
        elif inside:
            current.append(line)
    return blocks


@lru_cache(maxsize=None)
def get(name: str) -> str:
    file, heading, marker = REGISTRY[name]
    lines = (PROMPTS_DIR / file).read_text(encoding="utf-8").splitlines()
    for lang, text in _blocks(_section(lines, heading)):
        if lang != "json" and marker in text:
            return text[text.index("TASK:"):] if marker == "TASK:" else text
    raise KeyError(f"No prompt block containing {marker!r} under {heading!r} in {file}")


def prompt_hash(*names: str) -> str:
    return hashlib.sha256("\n\x00".join(get(n) for n in names).encode()).hexdigest()[:16]


_OPTIONAL = re.compile(r"\{([^{}]*)\{(\w+)\}([^{}]*)\}")


def render(template: str, **values) -> str:
    """Fill only the variables given. Optional segments like "{ on the topic: {topic}}" are kept
    only when that variable is given and non-empty; every other brace text is left untouched."""
    def optional(match: re.Match) -> str:
        value = values.get(match.group(2))
        return f"{match.group(1)}{value}{match.group(3)}" if value else ""

    text = _OPTIONAL.sub(optional, template)
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text
