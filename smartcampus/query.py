"""Step 8: rewrite follow-ups into standalone questions and split multi-question messages.
Used for search only; the generator always receives the student's original message."""
from __future__ import annotations

import json
import re

from . import prompts, schemas
from .evidence import attr
from .text import word_count

REFERENCE_START = re.compile(r"^\s*(it|this|that|these|those|they|them|why|and|also|what about|how about)\b", re.I)


def needs_rewrite(message: str, history: list[dict], quiz_context: dict | None) -> bool:
    return bool(history or quiz_context or message.count("?") > 1 or REFERENCE_START.search(message))


def format_history(history: list[dict], turns: int = 6, reply_chars: int = 300) -> str:
    """Last `turns` exchanges verbatim (replies trimmed); older ones reduced to the student's topics."""
    recent, older = history[-2 * turns:], history[:-2 * turns]
    lines = []
    if older:
        topics = [m["content"][:100] for m in older if m["role"] == "student"]
        lines.append("Earlier topics (summary): " + " | ".join(topics))
    for message in recent:
        text = message["content"] if message["role"] == "student" else message["content"][:reply_chars]
        mode = f" ({message['mode']})" if message.get("mode") else ""
        lines.append(f"{message['role'].upper()}{mode}: {text}")
    return "\n".join(lines)


def prepare_queries(llm, document: dict, message: str, history: list[dict],
                    quiz_context: dict | None = None, language: str = "English", force: bool = False) -> dict:
    """force=True rewrites even a standalone question (used when the evidence gate is unsure)."""
    fallback = {"needs_lecture": True, "questions": [{"standalone_question": message, "search_query": message,
                                                      "is_followup": False, "resolved": True}],
                "used_model": False}
    if not force and not needs_rewrite(message, history, quiz_context):
        return fallback
    sections = list(dict.fromkeys(p.get("section", "") for p in document["pages"] if p.get("section")))
    user = (f'<lecture name="{attr(document["name"])}" language="{attr(language)}">\n'
            f"<section_titles>{json.dumps(sections[:40], ensure_ascii=False)}</section_titles>\n</lecture>\n"
            + (f"<quiz_context>{json.dumps(quiz_context, ensure_ascii=False)}</quiz_context>\n" if quiz_context else "")
            + f"<conversation_history>{format_history(history) or '(none)'}</conversation_history>\n"
            f"<student_message>{message}</student_message>\n\n" + prompts.get("query_task"))
    try:
        response = llm.generate(prompts.get("query_system"), user, schema=schemas.QUERY, task="query")
    except Exception as exc:  # never block the student on this step
        return {**fallback, "error": type(exc).__name__}
    if not response.ok:
        return {**fallback, "error": response.error}
    out = response.parsed
    limit = 3 * word_count(message) + 40
    valid = (len(out["questions"]) <= 4 and (out["questions"] or not out["needs_lecture"])
             and all(3 <= word_count(q["search_query"]) <= 20 and word_count(q["standalone_question"]) <= limit
                     for q in out["questions"]))
    if not valid:
        return {**fallback, "error": "rewrite failed code checks"}
    return {**out, "used_model": True}
