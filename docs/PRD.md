# PRD: Traceable, Context-Aware Search for Regulated Technical Documents

**Event:** Autonomous SG 2026, Challenge #4
**Challenge question:** How might AI help people find, understand, and verify regulated technical information?
**Date:** 31 October 2026
**Status:** Draft v1

---

## 1. Problem

Mechanics and engineers need exact instructions from airworthiness directives (ADs), maintenance manuals, and accident reports, often under time pressure. A general-purpose LLM can blend instructions from different aircraft, serial ranges, or revisions, and state the result with confidence. In aviation, one wrong torque value, inspection interval, or applicability assumption can cause a serious mishap. The tool must find the right passage, apply the right conditions, show the source, and refuse when the answer is not certain.

## 2. Goal

Build a search and question-answering tool over multi-page technical documents that:
- returns only text that exists in the ingested documents,
- links every statement to its exact source location,
- evaluates applicability conditions (aircraft model, serial range, modification status) before answering,
- shows the document revision status and whether a newer version exists,
- refuses to answer when the evidence is missing, conflicting, or ambiguous,
- returns results in a structured format,
- responds within 3 seconds for queries over 1,000+ pages.

## 3. Users

- **Line mechanic (persona):** needs a specific inspection step for a specific tail number, quickly, and must know if it is current.
- **Airworthiness engineer (persona):** needs to check which directives apply to a serial range and whether a revision superseded an earlier one.
- **Quality or compliance reviewer (persona):** needs to audit each answer back to its source and see why the tool refused a question.

## 4. Scope

**In scope**
- Ingestion of PDF documents with page, section, and paragraph metadata.
- Structured extraction of applicability rules (aircraft models, serial ranges, modification or service bulletin conditions, effective dates).
- Hybrid retrieval (keyword and semantic) with metadata filters.
- Deterministic rule evaluation for applicability.
- Revision tracking: document ID, revision letter or date, and supersession links.
- Answer generation restricted to retrieved text, with per-sentence citations.
- Verification step that rejects any sentence not traceable to a source span.
- Refusal and uncertainty messages with reason codes.
- Structured output (JSON and rendered view).

**Out of scope**
- Authoritative approval of maintenance actions. The tool is a search and verification aid. It must always link to the official source document for the final decision.
- Live connection to manufacturer or regulator databases.
- Handling of handwritten, scanned-only, or image-only pages in the demo (flag them instead).

## 5. Data

**Provided dataset:** `AD Notice of Proposed Rulemaking (AD NPRM).xlsx`, exported as TSV (431 records, 22 columns). It lists FAA proposed airworthiness directives published in the Federal Register in 2026 and earlier. Each record has a docket number, subject heading, CFR part (430 are Part 39), issue and comment-due dates, make and model, product type and subtype, a link to the Federal Register notice, a page reference, and a plain-English summary of the proposed rule.

**What the data can and cannot support (profiled from the file):**

| Finding | Count | Implication for the PRD |
|---------|-------|-------------------------|
| Records with a summary | 430 of 431 | Summaries are the main text to index. They are short NPRM abstracts, not full maintenance manuals. |
| Records with a model list | 86 of 431 (345 empty) | Most rows lack model data. Applicability for these must come from the summary text or be marked MISSING_CONFIG. |
| Summaries mentioning serial numbers | 19 | Serial-range rules (the S-1000 to S-1500 example) can only be tested on a small subset. |
| Summaries mentioning supersession | 108 | Supersession links can be extracted from text for many records. |
| Docket numbers reused across rows | 323 unique docket numbers for 431 rows | Docket is not a unique key. Use docket plus page number or row ID. |
| Docket suffix "R1" (corrections) | 3 records, with OFR correction notes | Revision logic must handle "R1" dockets as corrected republications. |
| Comment-due date already passed (before 31 Oct 2026) | 394 records | The tool must show whether a comment period is still open, since a user may act on it. |
| Issue date before 2020 | 230 records | Old records must be flagged as potentially superseded or stale. |
| Status field | "Current" for all 431 | Status cannot be used to separate active from withdrawn rules. |
| Make field naming | Variants such as "Boeing" and "The Boeing Company", "Airbus" and "Airbus SAS" | Normalise manufacturer names before filtering. |
| Withdrawn notices in data | At least 1 (FAA-2024-1884 withdrawn) | The system must read the comments field and mark withdrawn notices. |
| Empty reference fields | AC Reference, AIR Policy Reference, Additional Reference all empty | Cannot be used for citation; rely on the Federal Register link and page number. |

**Important scope note:** this dataset contains proposed rules (NPRMs), not final airworthiness directives, and not maintenance manuals or accident reports. The tool must state this on every answer: a proposed rule is not a legally binding instruction. Where the challenge asks about maintenance manuals, the team will need a second corpus.

**Source for supplementary demo (if needed):** public final airworthiness directives from the FAA AD database. Record the exact document ID, revision, and download date for each file used.

**Corpus target:** at least 1,000 pages. The NPRM summaries alone do not reach this size, so either add the full Federal Register notices (the link column provides them) or add final ADs to reach the page target.

**Metadata stored per page and per paragraph:** document ID, title, revision, revision date, page number, section number, paragraph ID, applicability block (if found), and superseded-by link (if any).

## 6. Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| F1 | Ingest multi-page PDFs and store text with page and paragraph metadata | Must |
| F2 | Parse applicability statements into a structured rule (model, serial range, mod status, effective date) | Must |
| F3 | Evaluate rules for a user-supplied configuration (for example, aircraft model and serial number) | Must |
| F4 | Retrieve relevant passages within 3 seconds at 1,000+ pages | Must |
| F5 | Return each answer sentence with a citation (document ID, revision, page, paragraph) | Must |
| F6 | Show revision status: whether this is the latest revision, and what supersedes it | Must |
| F7 | Refuse to answer when evidence is missing, conflicting, or ambiguous, with a reason code | Must |
| F8 | Output structured JSON with a rendered view | Must |
| F9 | Show the highlighted source passage next to each citation | Should |
| F10 | Flag document pages where parsing confidence is low | Should |
| F11 | Export an audit log for each query | Should |

## 7. Architecture

The pipeline is split so that the LLM never decides facts on its own.

1. **Ingestion:** extract text from PDF pages, split into paragraphs, and attach metadata. Store the original page image reference.
2. **Rule extraction:** parse applicability text into a fixed rule schema. Where parsing is uncertain, store the rule as "unparsed" and flag it. Do not guess the rule.
3. **Index:** keyword index (BM25) and vector index over paragraphs, both filtered by metadata (document, revision, aircraft model).
4. **Query handling:** classify the query, extract the configuration (model, serial number), and apply the rule evaluator to filter candidate passages.
5. **Retrieval:** return the top passages that satisfy the applicability filter. Target latency: under 3 seconds end to end.
6. **Answer composition:** the model composes an answer only from retrieved passages, and must quote or closely paraphrase a passage for each sentence, with its citation.
7. **Verification:** a deterministic checker confirms each sentence maps to a retrieved passage (exact or high-overlap match). Sentences that fail are removed. If no sentence remains, the system refuses.
8. **Output:** structured JSON plus a rendered view, with citations, revision status, applicability result, and any uncertainty flags.

## 8. Applicability Rules

Each rule is stored in a fixed schema, for example:

- `applies_to_models`: list of aircraft models
- `serial_range`: start and end serial numbers, inclusive or exclusive as written in the source
- `mod_condition`: required or excluded modification or service bulletin
- `effective_date`: date from which the rule applies
- `source_ref`: document, revision, page, paragraph

**Evaluation behaviour:**
- If the user's configuration matches all conditions, the result is APPLIES.
- If any condition fails, the result is DOES NOT APPLY, with the failed condition shown.
- If the user's configuration is incomplete (for example, serial number missing), the result is UNKNOWN and the system asks for the missing field. It does not assume a default.
- If the rule text cannot be parsed, the result is UNPARSED and the system refuses to give the applicability answer.

Example from the challenge: Directive A applies only to serial numbers S-1000 to S-1500. A query for S-1600 returns DOES NOT APPLY with the rule and source. A query for S-1200 returns APPLIES with the source. A query with no serial number returns UNKNOWN.

## 9. Revision Logic

Each document has a revision identifier and a revision date. The system stores supersession links between revisions. For every answer, the output must include:
- the revision used for the citation,
- whether that revision is the latest known in the corpus,
- the newer revision ID if one exists.

If two revisions both apply and their content differs, the system refuses and lists both revisions. It does not pick one silently.

## 10. Refusal and Uncertainty Rules

The system must refuse, with a clear message and a reason code, in these cases:

| Reason code | Trigger |
|-------------|---------|
| `NO_EVIDENCE` | No retrieved passage supports any sentence of the answer |
| `OUT_OF_SCOPE` | Query asks about a document or aircraft not in the corpus |
| `CONFLICT` | Two passages or revisions give different instructions for the same configuration |
| `AMBIGUOUS_TERM` | Query uses a term that maps to more than one defined meaning or component |
| `MISSING_CONFIG` | Applicability needs a field the user did not give |
| `UNPARSED_RULE` | The applicable rule could not be parsed reliably |
| `VERIFICATION_FAILED` | Generated sentences failed the citation check |

A refusal message must state what is missing and what the user can provide to get an answer. It must not give a partial answer that looks complete.

## 11. Output Format

Every response is structured JSON:

```json
{
  "query": "...",
  "status": "ANSWERED | REFUSED",
  "reason_code": null,
  "configuration": {"model": "...", "serial_number": "..."},
  "applicability": {"result": "APPLIES", "rule_ref": "..."},
  "statements": [
    {"text": "...", "citations": [{"document_id": "...", "revision": "...", "page": 0, "paragraph": "..."}]}
  ],
  "revision_status": {"used": "...", "is_latest": true, "superseded_by": null},
  "latency_ms": 0
}
```

The rendered view shows the same content with citations as links to the highlighted source page.

## 12. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Retrieval latency | Under 3 seconds for queries over 1,000+ pages |
| Hallucination | Zero sentences in an answer without a verified source |
| Citation accuracy | Every citation resolves to the correct page and paragraph |
| Refusal accuracy | Refuse all queries in the test set marked ambiguous or out of scope |
| Transparency | All refusals and uncertainty flags shown to the user |

## 13. Evaluation

Build a test set of at least 30 queries across these types:
- answerable with clear applicability,
- applicability does not match the configuration,
- configuration incomplete,
- conflicting revisions,
- out of scope,
- ambiguous wording.

Report for each: correct status (answered or refused), correct citation, latency, and whether any sentence failed verification. Report failures honestly.

## 14. Four-Hour Build Plan

| Time | Task | Output |
|------|------|--------|
| 0:00 to 0:30 | Select public documents, record IDs and revisions, extract text | Corpus v1 |
| 0:30 to 1:15 | Build paragraph index with metadata; add BM25 search | Searchable corpus |
| 1:15 to 2:00 | Write rule schema and evaluator; parse 2 or 3 example directives | Applicability results |
| 2:00 to 2:40 | Build answer composition with citation requirement and verification checker | Verified answers |
| 2:40 to 3:15 | Add refusal reason codes and revision status | Refusal and revision output |
| 3:15 to 3:40 | Run test set, measure latency, record results | Evaluation table |
| 3:40 to 4:00 | README, LICENSE, demo recording | Public repo and demo |

## 15. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Rule parsing is incomplete in 4 hours | Parse only 2 or 3 directives fully; mark the rest UNPARSED and say so |
| Model still adds unsupported sentences | Verification checker removes them; refuse if nothing remains |
| Latency above 3 seconds | Pre-build indexes; cache embeddings; limit retrieval to top passages after metadata filter |
| Wrong use of the tool for real maintenance | Clear disclaimer on every output; always link to official source |
| Scanned PDFs with no text | Flag as unsupported in the corpus; do not OCR-guess in the demo |

## 16. Open Questions

1. Which public documents will the team use, and which revisions?
2. Does the team have a fixed budget for model calls during the demo?
3. Should the demo show the rendered view, the JSON, or both to judges?

---

*Note: the uploaded AD NPRM dataset (431 records) is the primary corpus, supplemented by public Federal Register notices or final ADs to reach the page target. Record document IDs, revisions, and download dates before the demo.*
