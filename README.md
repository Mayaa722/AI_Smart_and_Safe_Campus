# SmartCampus educational AI - team handoff

Start with [the notebook](SmartCampus/vscode/SmartCampus.ipynb). This portable
edition reads our actual saved results; all live switches are **off** initially.
The Streamlit app supports one uploaded PDF/PPTX at a time, a short summary,
comprehensive study guide, quiz, four tutor modes, source viewing and feedback.

## Run locally

Use Python 3.12. From this repository folder:

```powershell
py -3.12 -m venv SmartCampus/.venv
SmartCampus\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python handoff_setup.py
python -m streamlit run SmartCampus/vscode/streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

Open http://127.0.0.1:8501. For the notebook, select the interpreter in
`SmartCampus/.venv` in VS Code. macOS/Linux: create/activate the equivalent venv
with `python3.12 -m venv SmartCampus/.venv` and `source SmartCampus/.venv/bin/activate`.
New PPTX rendering uses desktop Microsoft PowerPoint on Windows; use an exported
PDF on other systems. Saved PowerPoint page renders are already included.

The signed-off original working folder on Abdullah's machine is unchanged.
This package needs no Tailscale, portable VS Code, browser profile or model weights.

## Where we left off

**All seven supplied test lectures are in scope: 360 pages.** Three were the first
batch, not the entire study. There is no completed two-provider quality ranking.

| Lecture | Gemini 3.5 | Groq-hosted Qwen 3.8 27B |
|---|---|---|
| IoT (50 pages) | Summary, full 25-chapter guide, 8 MCQs, 8 tutor cases; content flags remain | 40/50 pages processed |
| Calculus (33) | Summary; 16/17 chapters; quiz/tutor unfinished | 12/33 pages processed |
| Software requirements (46) | Summary, 6/23 chapters, 8 MCQs, 1 tutor case | Queued |
| Big data (86), Cloud (36), Data analysis (59), NLP (50) | Queued | Queued |

IoT's quiz includes a source-derived UDP-port-scan overclaim and weak distractors.
The practice quiz is gated, and the guide shows a separate source clarification.
The raw generated questions, original automated approvals and later assistant
findings are preserved. Software's eight keys passed assistant source review,
but distractors are often too easy. These are development reviews, **not new
instructor ratings or a participant study**. Linked videos were not ingested.

Both free API quotas were reached on 17 November. Recorded waits may now be
outdated: check current responses instead of assuming any retry time restores
the full allowance. Never delete ledgers, use new keys to bypass quotas, or
silently switch models. Local Qwen inference stays deferred and runs only in
Colab GPU, never the laptop.

## Resume the current work

Saved reading and local quiz grading need no key. For live generation put your
**own** key in `SmartCampus/vscode/.env`. Set only the matching free-plan flag to
`1` after checking that billing is disabled. Never commit this file.

```powershell
cd SmartCampus/vscode
python comparison/lecture_trials.py --arm groq
# When Gemini is available:
python comparison/lecture_trials.py --arm gemini
```

Use `--lecture iot` (or calculus, software, big_data, cloud, data_analysis, nlp)
to resume just one. Existing accepted work is reused. Source/image hashes,
request counters, previous failures, summaries and content flags are restored.
Opening the notebook or uploading a previously processed PDF does not itself
start a live generation run.

## Evaluation and remaining tasks

Four paper-aligned RAGAS families: faithfulness, answer relevance, context
precision and context recall. One new IoT direct answer has three scores near
1.0; answer relevance failed to finish. This is one text-context sample, not
a system-wide score. Source-checklist coverage and quiz validity are additional
checks. The fixed Gemini judge's same-family bias and source-review limitations
are documented. Checklists/reference answers for the four newly queued lectures
still need preparation before scoring them.

1. Resume missing provider work after quotas allow it; preserve all raw attempts.
2. Correct flagged teaching materials in a separately labelled reviewed edition.
3. Finish source checklists and matched evaluation across all seven lectures.
4. Obtain instructor review; then make a brief comparison with actual denominators,
   completion rates, errors and waiting time. Do not call functional checks metrics.

Evaluation has a separate dependency stack. Create a second Python 3.12 venv,
install root requirements plus `SmartCampus/vscode/evaluation/requirements.txt`,
then run `comparison/evaluate_lecture_trials.py` from the app directory only
when you deliberately want paid-provider-free-plan judge calls. The captured
scores are readable without installing RAGAS or making new requests.

Historical Colab work is retained in [the GPU notebook](notebooks/SmartCampus_Improved_Colab.ipynb)
and the restored `run/comparison_v2` input/runtime/result archives. These are
separate from the current seven-lecture experiment; do not mix their scores.

## Package contents and privacy

`lectures for testing/` contains the seven active PDFs. `SmartCampus/inputs/`
contains four earlier reference lectures. `checkpoints/` contains compressed,
checksummed saved state and original page images. `handoff_setup.py` restores
them and relocates operational paths to your checkout without rewriting raw
historical model records or resetting budgets. Rerunning setup preserves later
work. No API keys, personal venvs, real participant feedback, browser data,
Tailscale files or model weights are included. Service organisation IDs in
operational error metadata are redacted; generated answers are unchanged.

Lecture authorship/copyright stays with the respective course authors. These
materials are shared as project inputs, not claimed as team-authored content.
