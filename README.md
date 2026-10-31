# ✈️ AeroTrace

**Traceable, context-aware search over regulated aviation documents.**
Autonomous SG 2026 · Challenge #4 · AI & Aerospace track

> *Search with receipts:* every sentence comes with its source, and when the tool isn't sure, it refuses and says why.

AeroTrace answers questions about FAA Airworthiness Directive proposals (NPRMs). It:

- returns **only text that exists in the documents**, and cites document, revision, page and paragraph for every sentence
- **checks applicability with code, not the LLM**, e.g. "applies to serial numbers 0002 through 2519" gives APPLIES for S/N 1200, DOES NOT APPLY for 2600, and UNKNOWN (asks you) when no serial is given
- **tracks revisions**: shows the version used, whether a newer one exists, and whether the proposal was **withdrawn**
- **refuses with a reason code** when evidence is missing, conflicting or ambiguous
- returns **structured JSON** plus a rendered view, in single-digit milliseconds

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

streamlit run app.py                                                    # demo UI
python -m aerotrace.cli "Does the R22 directive apply to serial number 1200?"   # JSON in the terminal
python eval/run_eval.py                                                 # 38-query evaluation -> eval/results.md
pytest -q                                                               # unit tests
```

Set `ANTHROPIC_API_KEY` in `.env` to enable **Answer with Claude** in the semantic workflow
(see the index setup below). The **Verified answer** workflow also supports the legacy
**Let Claude write the answer** toggle. Its default extractive mode quotes source sentences
without making Claude API calls.

## OpenRouter embeddings and source preparation

The project includes a persistent two-stage semantic search: question embedding →
top N documents → top M chunks. It uses OpenRouter embeddings and displays both stages
in the Streamlit **Semantic evidence search** workflow. Its **Answer with Claude** button
connects the retrieved chunks to Claude through applicability/revision checks and source
verification. **Verified answer** retains the existing BM25 answer workflow as an alternative.

Install the added dependencies (PowerShell, from the project root):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Add these settings to your local `.env` (keep your actual key out of source control):

```dotenv
OPENROUTER_API_KEY=your-key
AEROTRACE_EMBEDDING_MODEL=openai/text-embedding-3-large
# Optional: AEROTRACE_EMBEDDING_DIMENSIONS=3072
```

The commands and Streamlit load the project's `.env`; existing environment variables take
precedence. Prefer `OPENROUTER_API_KEY`. Existing setups with an OpenRouter-format key in
`OPENAI_API_KEY` are also supported, including a project `.env` alias masked by a global
direct OpenAI key. Direct OpenAI keys are never sent to OpenRouter through that alias.
The OpenRouter key is separate from the existing optional `ANTHROPIC_API_KEY`.
Claude reads the current project's `.env` on each answer request; an explicit project
`ANTHROPIC_API_KEY` or model overrides inherited shell settings, so edits are picked up
without replacing process-wide secrets.

### Test the embedding API

This makes a small paid request and prints the model, number of results, vector dimensions,
and usage. It does not print the key or full vectors.

```powershell
.\.venv\Scripts\python.exe -m aerotrace.embeddings --text "Does this directive apply to the Robinson R22?" --text "This proposed directive concerns Robinson R22 helicopters."
```

Python callers can use `OpenRouterEmbeddings().embed(list_of_texts)` from
`aerotrace.embeddings`. The client batches requests, restores input order from response
indexes, validates vector dimensions/numbers, and retries transient errors with bounded
backoff. Blank inputs fail before sending a request. Oversized inputs are not truncated;
split them before embedding. This client does not persist vectors yet.

### Prepare the document sources

The active source is `data/challenge4_ad_index.xlsx`. Its `nprm_index` sheet is
exported to `data/ad_nprm.tsv`; the application and preparation command use this export.
The named `federal_register_link` field (column O in this workbook) mixes pasted
regulatory text and URLs. `summary` is column P. Re-export after replacing the workbook:

```powershell
.\.venv\Scripts\python.exe -m aerotrace.import_dataset
```

This also exports `revision_chains.tsv` and `data_notes.tsv`, and writes source/export
checksums to `data/dataset.json`. The original workbook is preserved unchanged.
First prepare every row offline using its available text and summary:

```powershell
.\.venv\Scripts\python.exe -m aerotrace.prepare
```

To download URL sources as well, start with a small selection:

```powershell
.\.venv\Scripts\python.exe -m aerotrace.prepare --fetch --docket 90-CE-35-AD --limit 1 --output .cache/aerotrace/prepared/sample.jsonl
```

Then prepare the complete corpus, including downloadable notices:

```powershell
.\.venv\Scripts\python.exe -m aerotrace.prepare --fetch
```

Output defaults to `.cache/aerotrace/prepared/documents.jsonl` (one document per line).
Each run replaces its chosen output with the selected records; use a separate `--output`
for samples. Original documents are cached under `.cache/aerotrace/sources/` and reused
on later runs. `--fetch --refresh` explicitly redownloads them. Cached sources are snapshots,
not a live check of the latest regulatory status.

Every prepared record retains its unique document ID, summary, manufacturer/model,
dates, revision label, citation/page range, source-row provenance, extracted text, and
source blocks. Source status is one of:

| Status | Meaning |
|---|---|
| `embedded` | Text was already present in `federal_register_link` |
| `downloaded` | Notice HTML/PDF was downloaded or read from the raw cache |
| `not_fetched` | A URL exists, but this was an offline run |
| `missing` | Column R has neither text nor a URL |
| `unavailable` | Download or reliable text extraction failed; the summary is retained |

Federal Register notice links are resolved through its public API to the notice body
or corresponding PDF when available. Other HTML landing pages are checked for document
PDF links. Unsupported, blocked, or unreadable documents are flagged instead of being
treated as evidence. No source host receives your OpenRouter key.

PDF blocks preserve physical PDF page numbers. HTML blocks preserve paragraph element
IDs and explicit Federal Register `data-page` values where provided. Embedded spreadsheet
sections and HTML without page metadata have no exact page number; the spreadsheet's
overall page range remains separate. Scanned PDFs are flagged; OCR is not performed.

Preparing documents does not call the embedding API. Build the semantic index separately
after preparing or updating the corpus.

### Build and search the two-stage semantic index

```powershell
# Build once; uses paid API calls only for texts absent from the local vector cache.
.\.venv\Scripts\python.exe -m aerotrace.retrieval build

# Embed a question and inspect both rankings.
.\.venv\Scripts\python.exe -m aerotrace.retrieval search "What could cause R22 teeter stop brackets to fail?" --top-n 20 --top-m 6

# Open the UI and choose Semantic evidence search.
.\.venv\Scripts\python.exe -m streamlit run app.py
```

The build reads `.cache/aerotrace/prepared/documents.jsonl`. It embeds each summary with
title/manufacturer/model/docket context. The one record without a summary explicitly uses
its metadata instead. It splits each source block separately into windows of at most
500 tokens with 64 tokens of overlap, preserving Unicode and source locations. Blocks
are never joined across documents, sections, or PDF pages. Summary and record-metadata
chunks are included and labelled separately from document-text chunks. The build does
not download missing full documents or silently treat summaries as full-text sources.

At query time, an explicit docket or `NPRM-...` record ID first restricts the eligible
documents to exact matches (including a base docket's known revisions). Unknown explicit
references produce an error before any embedding API request. Without an explicit reference,
all documents are eligible. A single question vector ranks eligible summaries using cosine
similarity. Stage 2 compares that same vector only against chunks belonging to the top N
documents and returns the top M chunks. Defaults are N=20 and M=6; both are adjustable.
If fewer eligible documents/chunks exist, the actual returned count is smaller.
There is no reranker or silent global-chunk fallback. Similarity is a ranking score, not
a calibrated confidence or an applicability result. Even an unrelated question has nearest
neighbours, so downstream evidence/refusal checks remain necessary before answering.

The index is saved under `.cache/aerotrace/retrieval/`:

- `manifest.json` identifies the current model, dimensions, corpus counts, and source hashes.
- Generation-specific JSON/NumPy files hold document/chunk metadata and normalized vectors.
- `vectors.sqlite3` caches vectors by model, dimensions, and exact text hash. Identical texts
  and repeated questions reuse vectors; changed text requires new embeddings. Failed builds
  retain completed cache batches and do not publish a partially built index.

The first tokenizer use downloads a public vocabulary into the project cache. Rebuilding
an unchanged index reuses cached vectors. Searches make no document-download calls and at
most one embedding request per new question. Cached questions need no embedding request.
The model and dimensions for questions come from the saved index, so changing `.env`
cannot accidentally compare incompatible vectors. The loader rejects an index when its
prepared corpus or exported dataset has changed; prepare and rebuild after updates.

Use `--chunk-tokens`, `--overlap`, `--batch-size`, `--input`, or `--index-dir` on `build`
to customize an index. `search --index-dir` selects it. Python callers can use
`TwoStageIndex().search(question, top_n=20, top_m=6)` from `aerotrace.retrieval`.
The returned JSON contains the candidate documents, selected chunk text/citations,
source-availability labels, embedding usage, and embedding/ranking latency separately.

### Generate a Claude answer from semantic evidence

Set `ANTHROPIC_API_KEY` in your local `.env`. The default answer model is
`claude-sonnet-5-5`; optionally set `AEROTRACE_MODEL` (or `ANTHROPIC_MODEL`) to another
Claude model your key can access. The client uses Anthropic's direct Messages API,
with a JSON output schema requiring a passage ID for each answer statement.

In Streamlit, choose **Semantic evidence search**, enter a question, and select
**Answer with Claude**. It performs retrieval automatically; **Search evidence** remains
available for inspecting results without a Claude call. Optional aircraft model, serial,
and part-number inputs are under **Aircraft configuration for answers** in the sidebar.
The answer shows its citations, rule/revision warnings, actual answer mode, and audit JSON.

```powershell
.\.venv\Scripts\python.exe -m aerotrace.cli "What does FAA-2026-11814 propose?" --retrieval semantic --llm --top-n 20 --top-m 6
```

The connected pipeline:

1. Checks ambiguous terms, unknown references, and missing aircraft configuration.
2. Restricts eligible records by explicit references or detected aircraft model, then
   performs the two-stage semantic search with the chosen N and M.
3. Selects one document/revision for composition, flags withdrawals and newer versions,
   and evaluates applicability deterministically. Conflicting/uncertain cases may refuse.
4. Sends that document's retrieved chunks and deterministic results to Claude. For a
   decisive applicability answer, the original rule passage may be added as explicitly
   recorded `supplemental_evidence`. Other summary passages are not silently added to
   semantic results. If a required revision has no retrieved chunks, increase M or name it.
5. Checks that each returned source ID exists, rejects unsupported numbers or statements
   failing the existing quote/word-overlap checker, and returns cited statements or a refusal.

Source text is treated as data, not instructions. Statements are returned as natural-language
sentences with citations. The deterministic word-overlap checker is a heuristic and does
not prove semantic correctness; inspect source passages for operational decisions.
The semantic ambiguity margin is also a conservative heuristic rather than calibrated confidence.
The final topic-coverage check ignores document-overview wording such as "What does
docket FAA-2026-11814 cover?" and uses record/docket IDs as source selectors. It still
checks substantive requests such as torque values or physical cover inspections.
`coverage_check` in the audit JSON reports its score and missing terms; a failure means
the draft did not address those terms, not proof that the entire source document lacks them.

Missing configuration or an out-of-scope reference stops before generation. API errors
are sanitized, and an extractive fallback is labelled with an `LLM unavailable` warning
and `mode: extractive`; it is never presented as a successful Claude response.
`llm_called`, `llm_model`, retrieval details, and the generated/removed statements are in
the audit JSON. A deterministic does-not-apply result can be returned without calling Claude.
Repeated Streamlit rerenders reuse the displayed answer; each explicit **Answer with Claude**
request can incur a new generation charge. Retrieval and overall answer timings are separate.

## How it works

```
question ──► parse configuration (model, serial, part, docket)       aerotrace/engine.py
         ──► refuse early: ambiguous abbreviation, unknown docket/aircraft
         ──► applicability filter: only docs whose rule could cover the model   aerotrace/rules.py
         ──► BM25 retrieval (+ optional vectors, RRF)                 aerotrace/index.py
         ──► ambiguity check (several different directives match equally)
         ──► revision logic (withdrawn? newer version? conflicting versions?)   aerotrace/revisions.py
         ──► deterministic rule evaluation: APPLIES / DOES_NOT_APPLY / UNKNOWN / UNPARSED
         ──► compose from ONE document only (extractive, or Claude)  aerotrace/compose.py
         ──► verify each sentence against its cited passage; drop failures       aerotrace/verify.py
         ──► coverage check: the answer must mention what was asked
         ──► JSON + rendered view                                     app.py
```

**The LLM never decides facts.** Applicability, revisions, refusals and verification are all plain Python.
Claude (optional) only rewrites retrieved passages. Any sentence that doesn't match its source (≥85% word overlap,
**and every number identical**) is removed. If nothing survives, the answer is refused with `VERIFICATION_FAILED`.

### Refusal reason codes

| Code | When |
|---|---|
| `NO_EVIDENCE` | Nothing in the corpus supports an answer, or the closest document doesn't mention what was asked |
| `OUT_OF_SCOPE` | Docket, AD, maker or model not in the corpus (e.g. "777X", "Cirrus") |
| `CONFLICT` | Two versions of a proposal both match and differ (ask for "latest" to resolve) |
| `AMBIGUOUS_TERM` | Abbreviation with several meanings ("MR", "TR"), or several directives match equally |
| `MISSING_CONFIG` | Applicability needs a model / serial / part number you didn't give |
| `UNPARSED_RULE` | The applicability text has a condition we couldn't parse, so we won't guess |
| `VERIFICATION_FAILED` | Generated sentences couldn't be traced to the source |

## Data

The active dataset is `data/challenge4_ad_index.xlsx`, exported by
`python -m aerotrace.import_dataset`. Its three sheets are preserved as runtime TSV files.
The earlier spreadsheet export has been replaced, rather than merged into this corpus.

| Finding | Impact |
|---|---|
| `nprm_index`: 431 records with stable IDs `NPRM-001` through `NPRM-431`; 430 summaries | Document and citation IDs use `record_id`; docket numbers can repeat |
| `federal_register_link`: 177 embedded-text records, 229 URL-only records, 25 empty | Embedded text is preserved; URL-only sources can be downloaded with `--fetch` |
| `revision_chains`: 207 entries across 99 repeated dockets | Joined by `record_id` and retained as dataset revision metadata; the engine also checks its own normalized revision groups |
| 62 records contain `supersedes_ad`; 16 have `mentions_serial_numbers=yes` | Preserved as supplied annotations; exact applicability still requires the source rule |
| **123 rows are withdrawal notices**, 5 revised proposals (SNPRM), 4 OFR corrections (`R1`) | Revision logic groups rows by base docket; withdrawn proposals are flagged |
| Docket numbers repeat (323 unique) | Use stable record IDs, not docket alone, as the document key |
| The new index omits comment deadlines, CFR-part fields, and free-form comments | Missing values stay unknown; they are not copied from the old dataset |
| Manufacturer names vary ("Boeing" / "The Boeing Company") | Normalised |
| The initial index has 2,320 passages (~1.08M characters) | Referenced Federal Register page ranges do not mean every page has been downloaded |

Rule extraction results: 157 rules fully parsed, 188 partial (model known, but an extra condition such as "certain
serial numbers per service bulletin" could not be parsed, so these give `UNPARSED_RULE` instead of a guess), 86 unparsed.

**Adding more documents:** put PDFs in `data/pdfs/` named like `2026-21-03_revA.pdf`. Each page/paragraph becomes a
citable passage; pages without extractable text are flagged rather than OCR-guessed.

## Evaluation

`python eval/run_eval.py`: 38 queries across answerable, applies, does-not-apply, missing config, conflicting
revisions, out of scope and ambiguous. Current result: **38/38** in extractive mode on the replacement dataset.
See [`eval/results.md`](eval/results.md).

Honest caveats:
- The eval set was written by the team while building the system, so it is not a blind test. Add your own queries.
- `python eval/bench_latency.py 20` replicates the corpus 20× (~44k passages): median 7 ms, max 26 ms.
- Model matching is conservative: "A330" will not be assumed to cover "A330-243" unless the rule lists the A330 series.

## Repo layout

```
aerotrace/      ingest.py  rules.py  revisions.py  index.py  compose.py  verify.py  engine.py  cli.py  text.py
app.py          Streamlit UI (answer view, highlighted sources, JSON, audit-log download)
eval/           queries.jsonl  run_eval.py  results.md  bench_latency.py
tests/          unit tests (incl. the challenge's S-1000..S-1500 example)
data/           challenge4_ad_index.xlsx, ad_nprm.tsv, revision_chains.tsv, data_notes.tsv, dataset.json, pdfs/
docs/           PRD.md, problem_statement.png
```

## Disclaimer

AeroTrace is a search and verification aid, not an approval of maintenance actions. The corpus contains **proposed**
rules (NPRMs), which are not legally binding. Always confirm against the official FAA / Federal Register document.

## License

MIT, see [LICENSE](LICENSE).
