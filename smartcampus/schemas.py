"""JSON Schemas for every model output, matching the schemas documented in prompts/*.md.

All objects list every property as required and forbid extra properties, so the same schema
works with strict structured-output modes (Gemini, Groq, Mistral, GitHub Models, Cerebras).
Property order matters: "plan" fields come first so the model writes them before the output.
"""
from __future__ import annotations

import copy


def obj(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
STRINGS = arr(S)
BLOOM = enum("remember", "understand", "apply", "analyze", "evaluate")

# ---------- features ----------
EVIDENCE_NOTE = obj({"slide_id": S, "quote": S, "use": S})


def explanation(version: str) -> dict:
    props = {}
    if version == "B":
        props["evidence_notes"] = arr(EVIDENCE_NOTE)
    props.update(answer=S, citations=STRINGS,
                 covered_by_lecture=enum("yes", "partly", "no"), follow_up=S)
    return obj(props)


OUTLINE_ITEM = obj({"heading": S, "slide_ids": STRINGS, "key_points": STRINGS})


def summary(version: str) -> dict:
    props = {}
    if version == "B":
        props.update(outline=arr(OUTLINE_ITEM), skipped_slides=STRINGS)
    props.update(title=S, body=S, citations=STRINGS, uncertainties=STRINGS)
    return obj(props)


QUESTION = obj({
    "stem": S, "options": STRINGS, "answer_index": I, "bloom_level": BLOOM,
    "difficulty": enum("easy", "medium", "hard"), "explanation": S,
    "distractor_rationales": STRINGS, "citations": STRINGS})
BLUEPRINT_ITEM = obj({"concept": S, "slide_ids": STRINGS, "bloom_level": BLOOM,
                      "key_fact_quote": S, "misconceptions": STRINGS})


def quiz(version: str) -> dict:
    props = {}
    if version == "B":
        props["blueprint"] = arr(BLUEPRINT_ITEM)
    props.update(questions=arr(QUESTION), notes=S)
    return obj(props)


# ---------- pipeline helpers ----------
DESCRIPTION = obj({
    "slide_id": S,
    "slide_type": enum("content", "title", "agenda", "section_divider", "administrative", "decorative_only"),
    "visual_elements": arr(obj({
        "type": enum("diagram", "flowchart", "chart", "plot", "table", "formula", "code",
                     "screenshot", "photo", "icon", "other"),
        "informative": B})),
    "description": S, "transcription": S,
    "tables": arr(obj({"caption": S, "markdown": S})),
    "search_terms": STRINGS, "text_conflicts": STRINGS, "unreadable": STRINGS})

QUERY = obj({
    "needs_lecture": B,
    "questions": arr(obj({"standalone_question": S, "search_query": S,
                          "is_followup": B, "resolved": B}))})

# ---------- checkers ----------
C1 = obj({"claims": arr(obj({
    "claim": S, "cited_ids": STRINGS,
    "label": enum("supported", "partially_supported", "not_supported", "contradicted", "additional_teaching"),
    "supporting_ids": STRINGS, "evidence_type": enum("text", "table", "visual_description", "none"),
    "quote": S, "reason": S}))})

C2 = obj({"points": arr(obj({
    "id": S, "label": enum("covered", "partial", "missing", "incorrect"),
    "candidate_quote": S, "reason": S}))})

OPTION_JUDGMENT = obj({"judgment": enum("correct", "incorrect", "cannot_tell"), "reason": S})
C3 = obj({
    "option_judgments": obj({k: OPTION_JUDGMENT for k in "ABCD"}),
    "defensible_options": arr(enum("A", "B", "C", "D")),
    "best_option": enum("A", "B", "C", "D", "none"),
    "verdict": enum("single_answer", "multiple_answers", "no_correct_answer", "not_answerable"),
    "supporting_ids": STRINGS, "quote": S, "stem_issues": STRINGS})

DISTRACTOR = obj({"label": enum("plausible", "weak", "also_correct", "unclear"), "reason": S})
C4 = obj({
    "answerable_from_cited": B,
    "explanation_correct": obj({"value": B, "reason": S}),
    "distractor_quality": arr(obj({"option": enum("A", "B", "C", "D"),
                                   "label": enum("plausible", "weak", "also_correct", "unclear"),
                                   "reason": S})),
    "cues": STRINGS,
    "stem_quality": obj({"label": enum("clear", "ambiguous", "negatively_worded"), "reason": S}),
    "bloom_level_actual": BLOOM, "bloom_matches": B, "trivial": B,
    "verdict": enum("pass", "revise"), "fixes": STRINGS})

CHECK = obj({"value": enum("true", "false", "not_applicable"), "reason": S})
C5_CHECKS = ["solution_withheld", "one_question", "attempt_addressed", "abstained_correctly",
             "injection_resisted", "answers_all_questions", "uses_history", "pedagogically_helpful"]
C5 = obj({"checks": obj({name: CHECK for name in C5_CHECKS})})

C6 = obj({"key_points": arr(obj({"id": S, "point": S, "slide_ids": STRINGS,
                                 "importance": enum("core", "detail")}))})

# ---------- repairs ----------
REPAIR_NOTES = arr(obj({"finding": S,
                        "action": enum("fixed", "removed", "kept_original", "marked_uncertain", "replaced"),
                        "reason": S}))


def with_repair_notes(schema: dict) -> dict:
    result = copy.deepcopy(schema)
    result["properties"]["repair_notes"] = REPAIR_NOTES
    result["required"].append("repair_notes")
    return result


def quiz_item_repair(version: str) -> dict:
    props = {"question": QUESTION}
    if version == "B":
        props["blueprint_item"] = BLUEPRINT_ITEM
    props["repair_notes"] = REPAIR_NOTES
    return obj(props)
