"""Build the data blocks that precede every task prompt, in the format documented in prompts/*.md."""
from __future__ import annotations

import json
from html import escape


def attr(value) -> str:
    return escape(str(value or ""), quote=True)


def slide_block(slide: dict, include_description: bool = True) -> str:
    lines = [f'<slide id="{attr(slide["source_id"])}" page="{slide["page"]}" section="{attr(slide.get("section", ""))}">',
             f"<text>{slide.get('text') or '(no extractable text)'}</text>"]
    for table in slide.get("tables", []):
        lines.append(f"<table>\n{table}\n</table>")
    if slide.get("notes"):
        lines.append(f"<speaker_notes>{slide['notes']}</speaker_notes>")
    description = slide.get("description") if include_description else None
    if description:
        for table in description.get("tables", []):
            lines.append(f'<table source="model-transcribed" caption="{attr(table["caption"])}">\n{table["markdown"]}\n</table>')
        body = description.get("description", "")
        if description.get("transcription"):
            body += "\nTranscription: " + description["transcription"]
        if description.get("text_conflicts") or description.get("unreadable"):
            body += "\nUncertain: " + "; ".join(description.get("text_conflicts", []) + description.get("unreadable", []))
        lines.append(f'<visual_description source="AI-generated from the slide image">{body}</visual_description>')
    lines.append("</slide>")
    return "\n".join(lines)


def evidence_block(lecture_name: str, slides: list[dict], include_descriptions: bool = True) -> str:
    ordered = sorted(slides, key=lambda s: s["page"])
    inner = "\n".join(slide_block(s, include_descriptions) for s in ordered)
    return f'<lecture_evidence lecture="{attr(lecture_name)}">\n{inner}\n</lecture_evidence>'


def feature_input(lecture_name: str, slides: list[dict], task: str, attached: list[str] | None = None,
                  history: str | None = None, quiz_context: dict | None = None,
                  student_request: str | None = None, include_descriptions: bool = True,
                  extra_blocks: list[str] | None = None) -> str:
    parts = [evidence_block(lecture_name, slides, include_descriptions),
             "<attached_images>" + (", ".join(attached) if attached else "none (text-only model or no images)") + "</attached_images>"]
    if quiz_context:
        parts.append("<quiz_context>" + json.dumps(quiz_context, ensure_ascii=False) + "</quiz_context>")
    if history is not None:
        parts.append(f"<conversation_history>{history or '(none)'}</conversation_history>")
    if student_request is not None:
        parts.append(f"<student_request>{student_request}</student_request>")
    parts += extra_blocks or []
    parts.append(task)
    return "\n\n".join(parts)
