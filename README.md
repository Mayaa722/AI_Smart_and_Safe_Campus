# SmartCampus — grounded study assistant (Maya_3_features)

Three features for one uploaded lecture (PDF, PPTX or DOCX):
**Explanation** (tutor chat with direct / hint / socratic / practice modes, follow-ups and quiz follow-ups),
**Summary**, and **Quiz** (answer everything first, then see results and ask about any question).

The pipeline combines the RAG design of Ostrowska et al. (IJCAI-26: embeddings + vector DB + reranker)
with Contextual Retrieval (context header per chunk), AI descriptions of slide images, hybrid search, an
evidence gate, and checking by a different model family. All prompts live in [prompts/](prompts/) and are
loaded from those Markdown files at run time.

## Pipeline

| Step | What | Code |
|---|---|---|
| 1 | Ingest: text, title, tables, notes and a page image per page | `smartcampus/ingest.py` |
| 2 | Describe slides that need it (vision model, cached, shared by all models) | `describe.py` |
| 3 | Slide types and sections | `structure.py` |
| 4–5 | Context headers; small-to-big chunks; tables never split mid-row | `chunking.py` |
| 6 | Index: bge-m3 embeddings in ChromaDB + BM25 (stopwords out, math symbols in) | `index.py` |
| 8 | Follow-up rewriting and question splitting (search only) | `query.py` |
| 9–10 | Dense + BM25 → RRF → reranker → slides + neighbours → admin filter → gate | `retrieve.py` |
| 11 | Features with prompt version A or B | `features/explain.py`, `summary.py`, `quiz.py` |
| 12 | Code checks + checkers C1–C6 | `checks.py`, `verify.py` |
| 13 | One repair per flagged item (R0–R3) | `repair.py` |
| 15 | Benchmark: M1 retrieval, M2 faithfulness, M3 coverage, M4 quiz validity | `evaluation/` |

All settings are in `config/settings.json`; models and their free-tier limits are in `config/models.json`;
retrieval ablations are in `config/ablations.json`.

## Setup (Python 3.12)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
copy .env.example .env      # then add your own API keys
```

- Check the model IDs and limits in `config/models.json` against your own accounts.
- The first search downloads bge-m3 and bge-reranker-v2-m3 (about 4.5 GB). Set `HF_HOME` in `.env` to choose where.
- PPTX/DOCX page images need LibreOffice (any OS) or Microsoft Office (Windows). Without them, files are read as text only.

## Use

```powershell
python -m smartcampus.cli ingest "data/lectures/iot lecture for testing.pdf"              # process a lecture (prints doc_id)
python -m smartcampus.cli ask <doc_id> "What is multipath?" --mode hint
python -m smartcampus.cli summary <doc_id> --version B
python -m smartcampus.cli quiz <doc_id> --n 8
python -m smartcampus.cli search <doc_id> "frequency bands"          # retrieval only, no generation
```

## Benchmark

**Start here:** [docs/SmartCampus_Evaluation_Guide.pdf](docs/SmartCampus_Evaluation_Guide.pdf) explains the metrics and
equations, the results so far (provisional), and how to repeat the evaluation with your own model.

| What | Where |
|---|---|
| Add your model | `config/models.json` (generator in `config/settings.json`; keep the checker unchanged) |
| Search setups (M1) | `config/ablations.json` |
| Test questions (drafts, under review) | `data/testsets/*.json` |
| Lecture PDFs (not in git) | copy them into `data/lectures/` with the same file names |
| Metric code | `smartcampus/evaluation/metrics.py`, `smartcampus/verify.py` |
| Your results | `runs/eval/` (created when you run a benchmark) |

Test-set format: [data/testsets/README.md](data/testsets/README.md).

```powershell
python -m smartcampus.cli check-testset data/testsets/iot.json
python -m smartcampus.cli eval-retrieval data/testsets/iot.json                          # M1, all ablations
python -m smartcampus.cli eval-retrieval data/testsets/iot.json --query-mode rewrite     # follow-up rewriting
python -m smartcampus.cli keypoints data/testsets/iot.json                               # draft key points (review!)
python -m smartcampus.cli eval-features data/testsets/iot.json --arms gemini-3.5-flash-lite groq-gpt-oss-120b openrouter-nemotron-super --versions A B
```

Reports (JSON + Markdown tables with n, mean and spread) are written to `runs/eval/`.
Every model response is cached in `runs/cache/`, so re-running a finished benchmark costs no quota;
use `--repeats 3` to measure run-to-run variation (the cache is bypassed for the generator).

The Streamlit app (`app/`) and the unit tests (`tests/`) are not in this branch yet.
