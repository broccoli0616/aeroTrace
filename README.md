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

Optional: `export ANTHROPIC_API_KEY=...` and toggle **"Let Claude write the answer"** in the UI (or pass `--llm` to the CLI).
Without a key, the tool runs in *extractive* mode: it quotes the best sentences word for word, so it can't hallucinate.

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

`data/ad_nprm.tsv`: 431 FAA AD NPRM records (exported from the provided spreadsheet). Findings from profiling it:

| Finding | Impact |
|---|---|
| 171 rows have the **full proposed-AD regulatory text** in the "Link for Submitting Public Comment" column | Main source for applicability sections, serial ranges and supersession links |
| **123 rows are withdrawal notices**, 5 revised proposals (SNPRM), 4 OFR corrections (`R1`) | Revision logic groups rows by base docket; withdrawn proposals are flagged |
| Docket numbers repeat (323 unique) | Document ID = docket + first FR page |
| `Status` is "Current" for every row; 3 reference columns are empty | Not used |
| Manufacturer names vary ("Boeing" / "The Boeing Company") | Normalised |
| Corpus spans 1,341 Federal Register pages (~1.07M characters of text indexed) | |

Rule extraction results: 152 rules fully parsed, 193 partial (model known, but an extra condition such as "certain
serial numbers per service bulletin" could not be parsed, so these give `UNPARSED_RULE` instead of a guess), 86 unparsed.

**Adding more documents:** drop a file in the sidebar under **📎 Add documents**. Accepted: PDF, Word (.docx),
Excel (.xlsx) and TSV, up to 50 MB; anything else (including renamed files) is rejected with a reason
(`aerotrace/uploads.py`). The review screen shows the docket, make, models, revision and detected applicability rule;
correct them, then **Confirm & add**. Only then is the document indexed and embedded (just its own passages; vectors are
cached per passage). Uploads are saved in `data/uploads/` (gitignored), reloaded on restart, and every citation from
them is labelled *uploaded, not verified against the Federal Register*. Spreadsheets in the FAA NPRM layout load like
`ad_nprm.tsv`; other tables become one passage per row. Rows identical to the official corpus are skipped as duplicates.
Scanned pages without extractable text are flagged rather than OCR-guessed. Set `AEROTRACE_UPLOAD_DIR` to store
uploads elsewhere. (PDFs placed directly in `data/pdfs/`, named like `2026-21-03_revA.pdf`, are still loaded too.)

## Evaluation

`python eval/run_eval.py`: 38 queries across answerable, applies, does-not-apply, missing config, conflicting
revisions, out of scope and ambiguous. Current result: **38/38** in extractive mode, median latency < 10 ms.
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
data/           ad_nprm.tsv, pdfs/
docs/           PRD.md, problem_statement.png
```

## Disclaimer

AeroTrace is a search and verification aid, not an approval of maintenance actions. The corpus contains **proposed**
rules (NPRMs), which are not legally binding. Always confirm against the official FAA / Federal Register document.

## License

MIT, see [LICENSE](LICENSE).
