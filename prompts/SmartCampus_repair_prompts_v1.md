# SmartCampus — repair prompts (v1)

Status: DRAFT for review. Used in **step 13 (Repair)**, only for items flagged in step 12 (Verify).

## Rules for all repairs
- **One repair attempt per item.** The repaired item is checked again with the same checker. If it is still flagged, it is shown as "Unreviewed" with its issues, or (for a quiz question) withheld until a teacher approves it.
- **Fix only what was flagged.** Valid content stays as it is, so a repair cannot silently rewrite good material.
- **Checkers can be wrong.** The repair model verifies each finding against the slides. If a finding is wrong, it keeps the original and says why in `repair_notes`.
- **Same system prompt as the features** (the shared system prompt in `SmartCampus_prompts_v1.md`), and the **same evidence block** the original generation received, plus any slides cited in the findings.
- **Same model as the generator** (the repair is a generation step). Checking stays with a different family.
- **One fixed version per repair prompt**, identical for every model arm.
- **Everything is logged:** original output, findings, repaired output and `repair_notes`. Originals are never deleted.
- **In the benchmark, report metrics before AND after repair**, plus the repair rate and the extra requests and time it costs. This shows how good raw generation is and how much repair adds.

| ID | Repair | Triggered by |
|---|---|---|
| R0 | Format repair | Code checks fail (invalid JSON, unknown slide ids, quotes not found, missing slides in outline, banned quiz phrases, word limits) |
| R1 | Explanation repair | C1 (contradicted or unsupported claims, wrong citations); C5 in benchmark runs (solution leaked, mode not followed) |
| R2 | Summary repair | C1 (contradicted or unsupported claims, wrong citations); code (outline misses content slides) |
| R3 | Quiz question repair | C3 (key not confirmed, several or no correct answers, not answerable); C4 (verdict "revise") |

## Shared repair output field
Every repaired output contains the feature's normal schema **plus**:
```json
"repair_notes": [ {
  "finding": "short copy of the finding it addresses",
  "action": "fixed | removed | kept_original | marked_uncertain | replaced",
  "reason": "string"
} ]
```
Every finding must have exactly one entry.

## Shared repair input block
```
{shared evidence block: <lecture_evidence>, <attached_images>, and for explanations <conversation_history>, <quiz_context>, <student_request>}
<original_output>{the flagged output, as JSON}</original_output>
<findings source="{checker id, e.g. C1 or code}">
[ {"id": "f1", "issue": "...", "details": "...", "slide_ids": ["..."]}, ... ]
</findings>
```
Code converts checker results into findings: for example, each C1 claim labelled `contradicted`, `not_supported` or `partially_supported`, or with citations that do not support it, becomes one finding with the claim text, label, reason and supporting slide ids.

---

## R0. Format repair (code-check failures)

```
TASK: Your previous output (in <original_output>) failed automatic checks, listed in <findings>. Return a corrected version.

- Fix each failed check exactly as described. Typical fixes: return valid JSON matching the schema; cite only slide ids that appear in <lecture_evidence>; copy quotes word for word from the slide text or table; place every content slide in exactly one outline entry; remove banned phrases; respect word limits.
- Do not change content that did not fail a check.
- Fill repair_notes with one entry per finding.
```

---

## R1. Explanation repair

```
TASK: Repair the tutor reply in <original_output> so that it fixes the problems in <findings> while still answering <student_request> in teaching mode {mode}.

FOR EACH FINDING
1. Check it against <lecture_evidence>. If the finding is wrong (the slides do support the original statement), keep the original text and record "kept_original" with the reason.
2. If the statement is contradicted by the slides: correct it to match the slides and cite the slide that supports the correction ("fixed").
3. If the statement is not supported by any slide: remove it ("removed"), or, if it is useful background knowledge that does not contradict the slides, keep it with the label "Additional teaching:" and no citation ("fixed").
4. If a detail is overstated or missing a condition: narrow it to exactly what the slide says ("fixed").
5. If the citation is wrong but the statement is correct: replace the citation with the slide that actually supports it ("fixed").
6. If the reply revealed the solution in hint, socratic or unsolved practice mode: rewrite it to follow that mode's rules, giving a cue or guiding question instead of the answer ("fixed").
7. If the slides cannot settle the point: say so in the reply ("marked_uncertain").

KEEP
- All statements that were not flagged, the reply's structure, the teaching mode and its length limits.
- covered_by_lecture must still be accurate after the repair; if removing statements leaves the question unanswered, say the lecture does not cover it and set covered_by_lecture accordingly.
- In version-B output, update evidence_notes so that every cited id and every fact used still appears there with an exact quote.
- If <quiz_context> is present, the quiz follow-up rules still apply.
```

---

## R2. Summary repair

```
TASK: Repair the lecture summary in <original_output> so that it fixes the problems in <findings>.

FOR EACH FINDING
1. Check it against <lecture_evidence>. If the finding is wrong, keep the original text and record "kept_original" with the reason.
2. Contradicted statement: correct it to match the slides, with the supporting citation ("fixed").
3. Unsupported statement: remove it ("removed"). Do not keep it as "Additional teaching": summaries contain lecture content only.
4. Overstated or incomplete detail: narrow it to exactly what the slide says ("fixed").
5. Wrong citation on a correct statement: replace it with the slide that actually supports it ("fixed").
6. Missing topic (content slides not covered): add a heading or bullet for it in lecture order, with citations ("fixed").
7. Unreadable or ambiguous source content: move it to uncertainties ("marked_uncertain").

KEEP
- All bullets that were not flagged, the structure (Overview → topics in lecture order → Key takeaways) and the length limit of 350–650 words (never more than 800). If additions would exceed it, shorten the least important unflagged bullets rather than dropping a topic.
- About 8–20 citations in total; every cited id must exist in <lecture_evidence>.
- In version-B output, update outline and skipped_slides so that every content slide still appears in exactly one outline entry.
```

---

## R3. Quiz question repair

**Extra input:**
```
<item_results>
{"blind_solver_1": {C3 output}, "blind_solver_2": {C3 output}, "review": {C4 output}}
</item_results>
```
The original item and its blueprint entry (version B) are in `<original_output>`. The other questions in the quiz are listed in `<other_stems>` so that the repair does not duplicate them.

```
TASK: Repair the single multiple-choice question in <original_output> using the problems found in <findings> and <item_results>.

FIRST, DIAGNOSE (using the slides, not the checkers' opinions alone):
- Is the stored key really correct according to the cited slides?
- Is exactly one option defensible?
- Is the question answerable from the cited slides?
If a finding is wrong, keep that part unchanged and record "kept_original" with the reason.

THEN APPLY THE SMALLEST FIX THAT WORKS:
1. Wrong key, but the question and options are otherwise good, and the slides clearly support exactly one other option: change answer_index to that option and rewrite the explanation and distractor_rationales to match ("fixed"). State the change in repair_notes; the item will be re-checked blind.
2. Two options defensible ("also_correct"): rewrite the extra correct option into a plausible but clearly wrong misconception ("fixed").
3. Weak distractor: replace it with a realistic misconception or a confusion with a neighbouring concept from THIS lecture ("fixed"). Do not reuse the rejected wording.
4. Cue that reveals the key (length, repeated stem words, grammar, absolutes): rebalance the options so that all four are parallel in grammar and similar in length ("fixed").
5. Ambiguous or negatively worded stem: rewrite it as a clear, positively worded question ("fixed").
6. Explanation wrong or unsupported: rewrite it from the slides, with citations ("fixed").
7. Not answerable from the cited slides, or trivia: write a NEW question on the same concept (or the closest important concept on the same slides) that is answerable from them, at the same Bloom level if possible ("replaced"). It must not duplicate any stem in <other_stems>.
8. If no good question can be written from these slides: return the item unchanged and record "removed" with the reason. The question is then dropped from the quiz.

ALL QUIZ RULES STILL APPLY
- Exactly 4 options and one correct answer; positively worded stem; parallel, similar-length options; no all/none of the above, "both A and B", absurd options or absolutes used only in wrong options; no slide ids in the stem or options.
- explanation with [id] citations; distractor_rationales for each wrong option, in option order, skipping the key; bloom_level and difficulty.
- In version B, update the blueprint entry (key_fact_quote must be an exact quote that determines the new key).
```

**After R3:** the repaired item goes through C3 (both blind solvers) and C4 again. If it still fails, it is withheld from students until a teacher approves or edits it. A quiz with withheld items is shown with fewer questions, and the student is told how many.
