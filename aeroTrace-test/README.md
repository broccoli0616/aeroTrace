# AeroTrace

AeroTrace is a traceable search prototype for the FAA proposed airworthiness directives in
`challenge4_ad_index.xlsx`. It answers what a supplied proposal says, checks extractable aircraft
applicability conditions, and ties every generated claim to an exact official Federal Register passage.

The corpus contains proposals. The app does not determine which final AD currently governs an aircraft.
“Latest” means latest among supplied records sharing a docket.

## Current corpus

- 431 supplied index records and 431 resolved official notices
- 5,651 section-aware, page-aware passages
- SQLite FTS5 keyword index
- 1,536-dimensional OpenRouter `text-embedding-3-small` vectors
- Cached source documents, vectors, and metadata for repeatable local use

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/build_index.py
streamlit run app.py
```

The populated `.env` must contain `ANTHROPIC_API_KEY` and an OpenRouter key in `OPENAI_API_KEY`.
Generated sources and indexes live under `data/raw/` and `artifacts/` and are ignored by Git.

The build is resumable. It reuses official source files and vectors whose text hash and model match.

## Question path

1. Parse docket, AD, model, serial, and part identifiers from the question and optional UI fields.
2. Apply exact document and known-model filters.
3. Run FTS5 keyword search and OpenRouter semantic search, merged with reciprocal-rank fusion.
4. Select one notice and add its applicability passage to the evidence pack.
5. Evaluate conservative model and serial conditions in Python.
6. Ask Anthropic to return structured claims, each with one passage ID and verbatim evidence quote.
7. Reject unknown passage IDs, absent quotes, and numbers not present in the cited quote.

## Verification

```bash
pytest -q
python eval/run_retrieval.py --split heldout --keyword-only
python eval/run_retrieval.py --split heldout
python scripts/ask.py "What inspection does FAA-2026-11814 propose?"
```

The held-out targets are available only to the evaluation runner; the search engine receives the
question, never the target document ID.

See [`eval/results.md`](eval/results.md) for measured results and limitations.

