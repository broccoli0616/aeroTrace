# ✈️ AeroTrace

**Traceable, context-aware search over aviation technical documents.**

Autonomous SG 2026 · Challenge #4 · AI & Aerospace track

AeroTrace helps engineers find technical passages, check whether a directive applies to an aircraft, and inspect the evidence behind an answer. It combines keyword and optional semantic retrieval with applicability rules, revision tracking, and per-statement citations.

The bundled corpus contains FAA **Notice of Proposed Rulemaking (NPRM)** records. These are proposals, not final maintenance instructions. AeroTrace is a search and verification aid; users must check the authoritative source before acting.

## Why this project

An answer about the wrong aircraft, serial range, or revision can be misleading even when it quotes a real document. Challenge #4 asks for a search engine that makes those conditions and its evidence visible, and refuses when information is insufficient.

| Challenge requirement | Current implementation |
|---|---|
| Specific source references | Document, revision, passage identifier, and available page/section metadata for each answer statement |
| Configuration filtering | Model-based retrieval filtering followed by rule evaluation for model, serial range, and supported part-number conditions |
| Revision awareness | Docket-based revision grouping, supersession references, withdrawal warnings, and conflict handling |
| Clear uncertainty | Refusal codes, missing-field prompts, and unparsed-rule warnings |
| Structured results | Streamlit answer view, JSON view, and downloadable per-query audit JSON |
| Reduce unsupported answers | Original-text extraction by default; optional Claude drafts checked against cited passages |
| Under 3 seconds over 1,000+ pages | Target to validate with a representative full-text corpus and intended API configuration |

See the [problem statement](docs/problem_statement.png) and [product requirements](docs/PRD.md).

## Quick start

Run from the repository root. No API key is required for keyword search and extractive answers.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open the local URL printed by Streamlit, usually `http://localhost:8501`.

For JSON in the terminal:

```bash
python -m aerotrace.cli "Does the R22 directive apply to serial number 1200?" --full
```

If an existing `.env` enables embeddings, startup uses that configuration. To explicitly run without embedding API calls:

```bash
AEROTRACE_EMBEDDINGS= python -m streamlit run app.py
```

## API configuration

Embedding and answer generation are separate features. An embedding key enables semantic retrieval; it does not enable Claude answer writing.

| Mode | Retrieval | Answer composition | Credentials |
|---|---|---|---|
| Default | Local BM25 | Extract source sentences | None |
| Hybrid retrieval | BM25 + vectors, merged with reciprocal rank fusion (RRF) | Extract source sentences | Embedding provider key, or optional local model |
| Claude answers | Either retrieval mode | Claude draft followed by verification | Anthropic key; embedding credentials only if also enabled |

Create a `.env` file **beside `app.py`**:

```bash
cp -n .env.example .env
```

Edit `.env` in your editor. The app loads it when started from the repository root; existing shell variables take precedence. Lines beginning with `#` are comments. Restart after changing settings.

### OpenRouter embeddings

```dotenv
OPENAI_API_KEY=replace_with_your_openrouter_key
AEROTRACE_EMBEDDINGS=openai
AEROTRACE_EMBED_MODEL=openai/text-embedding-3-small
OPENAI_BASE_URL=https://openrouter.ai/api/v1
```

The variable is named `OPENAI_API_KEY` because the code uses an OpenAI-compatible client. Keys beginning with `sk-or-` automatically select the OpenRouter endpoint if `OPENAI_BASE_URL` is unset.

### OpenAI embeddings

Use this configuration instead of the OpenRouter block:

```dotenv
OPENAI_API_KEY=replace_with_your_openai_key
AEROTRACE_EMBEDDINGS=openai
AEROTRACE_EMBED_MODEL=text-embedding-3-small
OPENAI_BASE_URL=https://api.openai.com/v1
```

For another compatible provider, supply its base URL and supported model name. The client calls the embeddings endpoint under that base URL; do not use a model's website URL.

At initialization, uncached passage texts are sent to the provider. Vectors are cached per passage in `.cache/`; each subsequent search needing semantic retrieval sends the query text for embedding. These calls can incur provider charges. Startup and query latency depend on the provider and cache state.

Inspect the answer's JSON `retrieval` field. `bm25+openrouter/...` or `bm25+openai/...` indicates a hybrid index initialized; `bm25` indicates keyword-only retrieval. A query embedding can still fail and fall back to BM25 after initialization, so also check terminal logs. This field describes the initialized backend, not per-request health.

### Optional local embeddings

```bash
python -m pip install sentence-transformers
AEROTRACE_EMBEDDINGS=1 python -m streamlit run app.py
```

This selects `all-MiniLM-L6-v2` without an embedding API key. The model may need to download on first use.

### Optional Claude answers

Add a direct Anthropic credential to `.env`:

```dotenv
ANTHROPIC_API_KEY=replace_with_your_anthropic_key
```

Enable **Let Claude write the answer** in the sidebar, or pass `--llm` to the CLI. `AEROTRACE_MODEL` overrides the model selected in [compose.py](aerotrace/compose.py). Compatible model access and SDK support are required; offline tests do not validate live Claude calls.

Leave `ANTHROPIC_API_KEY` empty for extractive answers. An OpenRouter key does not configure the direct Anthropic client.

Keep real keys in `.env` or deployment secrets, never in `.env.example`. `.env` is ignored by Git.

## How an answer is produced

```text
Question + optional model / serial / part number
    ↓
Parse configuration; check recognized ambiguous or out-of-scope requests
    ↓
Filter candidate documents by explicit references or aircraft model
    ↓
BM25 + optional semantic retrieval → RRF passage ranking
    ↓
Choose a document; check ambiguity, revisions, and applicability
    ↓
Extract original sentences OR ask Claude to draft from its passages
    ↓
Check citations, source-text overlap, numbers, and topic coverage
    ↓
Answer with evidence OR refusal with a reason code
```

The composer uses passages from one selected document to limit blending of directives. Applicability evaluation returns `APPLIES`, `DOES_NOT_APPLY`, `UNKNOWN`, or `UNPARSED`. General information questions can receive an answer with an applicability warning; explicit applicability questions require sufficient configuration and a supported rule.

The verifier accepts normalized exact quotes. For paraphrases, it requires at least 85% token overlap and rejects numeric tokens absent from the cited passage. This is a **heuristic check**, not proof of semantic correctness: it cannot detect every negation, wrong relationship, or number assigned to the wrong component.

Hybrid passage retrieval is implemented, but document selection still ranks by BM25 scores. Evidence thresholds and default sentence extraction also remain keyword-based. Embedding-only matches can therefore still be refused. Semantic improvements need end-to-end evaluation.

### Refusal codes

| Code | Meaning |
|---|---|
| `NO_EVIDENCE` | Retrieved text does not adequately support an answer |
| `OUT_OF_SCOPE` | A recognized document or aircraft reference is absent |
| `CONFLICT` | Candidate revisions cannot be resolved by current rules |
| `AMBIGUOUS_TERM` | An abbreviation or competing directives need clarification |
| `MISSING_CONFIG` | Required aircraft configuration is missing |
| `UNPARSED_RULE` | Applicability conditions cannot be evaluated reliably |
| `VERIFICATION_FAILED` | Drafted statements fail source checks |

## Upload documents

1. Under **📎 Add documents**, select a text PDF, Word `.docx`, Excel `.xlsx`, or `.tsv` file, up to 50 MB.
2. Review the extracted information. For uploads producing up to five document records, edit title, docket, make, models, and revision as needed; larger uploads show a summary table.
3. Click **Confirm & add** to save and index the document. With API embeddings enabled, uncached text is embedded at this step.
4. Ask a question and expand **Show highlighted source passage**. Exact answer strings are highlighted in the extracted text.

PDF citations retain page metadata; Word citations use section/body/table locations because Word has no fixed pagination. Spreadsheets use available sheet/row or source-record metadata. The provided FAA layout has a specialized importer; other tables become passages built from column names and values. Scanned-only PDFs are not OCR-processed.

Uploads are labelled **user-provided, not verified against the Federal Register**. Repeated file bytes are detected by hash, and rows matching the existing corpus are checked for duplicates when added.

### Storage

| Data | Location |
|---|---|
| Bundled corpus | `data/ad_nprm.tsv`; optional PDFs in `data/pdfs/` |
| Confirmed uploads | `data/uploads/<hash-prefix>__<filename>` |
| Original filename, upload time, review edits | Companion JSON in `data/uploads/` |
| Parsed documents, passages, BM25 index | Application memory; rebuilt on startup |
| API embedding vectors | Per-passage cache in `.cache/` |
| Query audit output | JSON downloadable from the JSON tab |

`AEROTRACE_UPLOAD_DIR` overrides the upload folder. Local uploads reload on restart while their files remain on disk. No database service is required.

Uploads are shared across users of the same app instance, including removal. Removing an upload deletes its saved file and metadata and removes it from the active index; cached embedding vectors are not currently purged. On a host with ephemeral storage, uploads and caches are not durable across instance replacement.

## Corpus and scope

The supplied spreadsheet is exported as [data/ad_nprm.tsv](data/ad_nprm.tsv), containing 431 records. The base engine previously loaded 2,199 citable passages; uploads change that total. Records include summaries, available regulatory text, source links, and revision-related notices. This is not a complete collection of maintenance manuals or accident reports.

The earlier corpus profile reports references spanning 1,341 Federal Register pages. That is **not evidence that 1,341 complete PDF pages have been ingested and benchmarked**. A representative 1,000+ page full-text validation remains necessary for the challenge's scale target.

“Latest” means latest known in the loaded corpus, not a live FAA status check. Comment-period calculations use the fixed hackathon date, **31 October 2026**, in [revisions.py](aerotrace/revisions.py). Status/disclaimer logic is NPRM-oriented and needs extension before treating arbitrary uploads as final directives or manuals.

## Demo walkthrough

Try these with the bundled corpus and no aircraft overrides in the sidebar:

| Question | Demonstrates |
|---|---|
| `Does the R22 directive apply to serial number 1200?` | Supported serial range, cited rule, and withdrawal warning |
| `Does the R22 directive apply to serial number 2600?` | `DOES_NOT_APPLY` with the failed condition |
| `Does the R22 directive apply to my helicopter?` | `MISSING_CONFIG` rather than an assumed serial number |
| `What does the revised proposal FAA-2026-1338 require for A330?` | Revision conflict refusal |
| `What is the MR servo inspection?` | Ambiguous abbreviation refusal |
| `What is the torque for the Boeing 777X flap track?` | Out-of-scope refusal |

Show the source passage, applicability result, revision status, and JSON. Then upload a text PDF and demonstrate the review-to-search workflow. Results can change with the corpus and retrieval configuration.

## Tests and evaluation

```bash
# Offline tests: conftest.py disables live embedding calls.
python -m pytest -q

# BM25 + extractive evaluation; writes eval/results.md.
# Use --out to select another report path.
AEROTRACE_EMBEDDINGS= python eval/run_eval.py

# Synthetic benchmark: repeats the existing corpus 20 times.
AEROTRACE_EMBEDDINGS= python eval/bench_latency.py 20
```

The last verified local test run passed **46 tests**, covering rules, verification, mocked embeddings, uploads, and Streamlit interactions. The checked-in [evaluation report](eval/results.md) records **38/38** passing queries in extractive mode. It checks status, selected refusal codes, applicability, and expected cited docket where specified.

These are team-authored tests, not an independent assessment. They do not establish zero hallucination, complete page-level citation accuracy, or live API reliability. Repeating existing text is not a substitute for testing diverse documents. Measure live embedding and Claude latency separately from the offline baseline; query timings exclude initial index construction.

## Deployment configuration

Use `app.py` as the Streamlit entry point and install `requirements.txt`. The repository includes a [secrets template](.streamlit/secrets.toml.example); configure credentials through the host's secrets settings. The app copies supported top-level secrets into environment variables.

Set `APP_PASSWORD` for the shared demo password gate. This is not individual user authentication or document-level permissions. For Streamlit Community Cloud, treat the local upload directory and cache as temporary and keep source-file backups outside the app.

## Troubleshooting

| Symptom | Next step |
|---|---|
| `.venv/bin/activate` does not exist | Run `python3 -m venv .venv` first |
| Missing `pypdf`, `openpyxl`, or `docx` | Activate `.venv`, then run `python -m pip install -r requirements.txt` |
| Wrong Python environment | Run `./.venv/bin/python -m streamlit run app.py` from the repository root |
| JSON says `bm25` despite an embedding key | Check `.env`, uncomment settings, restart, and inspect terminal errors |
| OpenRouter model error | Check the model name, including its vendor prefix |
| Claude toggle disabled | Configure a direct `ANTHROPIC_API_KEY` and restart |
| Uploaded spreadsheet reported as duplicate | Its records may already be in the bundled corpus |

## Repository map

```text
app.py                  Streamlit interface and password gate
aerotrace/
  ingest.py             Base corpus and PDF loading
  uploads.py            Upload parsing, review edits, disk storage
  index.py              BM25, embeddings, vector cache, RRF
  engine.py             Query pipeline and corpus management
  rules.py              Applicability extraction and evaluation
  revisions.py          Revision grouping and status
  compose.py            Extractive answers and optional Claude integration
  verify.py             Source-text checks
  cli.py                JSON command-line interface
data/                   Bundled corpus and local document folders
docs/                   Problem statement and PRD
eval/                   Query set, reports, benchmarks
tests/                  Automated tests
```

## Next validation priorities

- Compare BM25 and hybrid retrieval on independently written paraphrases and exact model/part-number queries.
- Validate citations and retrieval latency on 1,000+ actual full-text pages, reporting API timing separately.
- Test negation, mixed units, component/value mismatches, and conflicting instructions beyond token overlap.
- Improve supported applicability conditions and source-page navigation, retaining refusals for unsupported conditions.

## License

Project code is released under the [MIT License](LICENSE). Source documents retain their respective terms.
