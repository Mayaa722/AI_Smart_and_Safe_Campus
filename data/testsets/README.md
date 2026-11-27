# Test sets (for the benchmark)

One JSON file per lecture, e.g. `data/testsets/iot.json`. Put the lecture file itself in `data/lectures/`
(lecture files are not committed to git).

Check a file with:
```
python -m smartcampus.cli check-testset data/testsets/iot.json
```

## Format

```json
{
  "lecture_file": "iot.pdf",
  "lecture_name": "IoT – Lecture 3",
  "subject": "stem",
  "questions": [
    {
      "id": "q01",
      "question": "What is the difference between a LAN and a WAN?",
      "type": "keyword",
      "gold_pages": [3],
      "reference_answer": "A LAN covers a small area such as a building; a WAN connects networks over large distances.",
      "key_points": ["LAN covers a small geographic area", "WAN spans large distances and connects LANs"],
      "mode": "direct"
    },
    {
      "id": "q02",
      "question": "and why does it need routers?",
      "type": "followup",
      "gold_pages": [4, 5],
      "history": [
        {"role": "student", "content": "What is a WAN?"},
        {"role": "tutor", "content": "A WAN connects networks over large distances..."}
      ]
    },
    {
      "id": "q03",
      "question": "What was Apple's closing stock price yesterday?",
      "type": "out_of_scope",
      "gold_pages": []
    }
  ]
}
```

## Fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique id, e.g. `q01` |
| `question` | yes | Exactly what a student would type |
| `type` | yes | `keyword` (uses the slide's words), `paraphrase` (different words), `visual` (answer is in a diagram/image), `table`, `math`, `followup` (needs `history`), `multi` (several questions in one message), `out_of_scope` (not in the lecture), `other` |
| `gold_pages` | yes | Page numbers (1 = first page of the file) that contain the answer. Empty only for `out_of_scope`. |
| `reference_answer` | no | A correct answer written by a person ("with answers" questions) |
| `key_points` | no | Short checkable facts the answer must contain (used for M3 coverage). If you write a reference answer, splitting it into 2–5 key points is enough. |
| `mode` | no | `direct` (default), `hint`, `socratic` or `practice` |
| `history` | for `followup` | Earlier turns: `{"role": "student" or "tutor", "content": "..."}` |

## Tips for a good test set (per lecture)
- About 25–40 questions, with a mix of types: roughly 30% keyword, 30% paraphrase, 15% visual/table/math,
  15% follow-up/multi, 10% out-of-scope.
- Questions **without** answers still work for retrieval (M1), faithfulness (M2) and mode checks;
  questions **with** reference answers or key points also give coverage (M3).
- Mark `gold_pages` carefully, using the page number in the file, not the number printed on the slide.
- Lecture key points for summaries are drafted with `python -m smartcampus.cli keypoints data/testsets/iot.json`.
  A teammate must review that draft and set `"reviewed": true` before it is used.
