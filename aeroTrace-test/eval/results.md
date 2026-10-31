# Evaluation results

Run on 31 October 2026 against 431 fetched official notices and 5,651 indexed passages.

| Retrieval mode | Held-out questions | Top-5 document recall | p50 | p95 |
|---|---:|---:|---:|---:|
| SQLite FTS5 keyword baseline | 10 | 90% | 3.2 ms | 19.3 ms |
| Keyword + OpenRouter embeddings | 10 | 100% | 1,368.0 ms | 1,905.3 ms |

The target document IDs are held outside the application and are not passed to retrieval. The held-out
questions cover direct identifiers, natural-language component descriptions, similar aircraft notices,
and a withdrawal. The set was created during development and is not an independent benchmark.

One live end-to-end query, “What inspection does FAA-2026-11814 propose?”, selected `NPRM-001` and
returned three validated claims. Retrieval took 1,405.8 ms and Anthropic synthesis took 6,532.7 ms.
Two generated claims were removed by the citation validator. This demonstrates that retrieval meets the
sub-three-second target on this corpus; total answer generation is slower and is reported separately.

## Known limits

- The corpus consists of proposed rules. It does not establish the currently effective final AD.
- Applicability evaluation is deliberately conservative. Conditions delegated to external service
  information return unknown unless they can be parsed from the notice itself.
- Citation validation verifies the cited passage, verbatim evidence quote, and exact numbers. It is not
  a complete natural-language entailment proof.
- The 10-question held-out set is small. Expand it with independently authored questions before making
  broader accuracy claims.

