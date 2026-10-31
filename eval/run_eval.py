"""Run the evaluation set and print a results table. Usage: python eval/run_eval.py [--llm] [--out eval/results.md]"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aerotrace.engine import get_engine  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--out", default=str(Path(__file__).parent / "results.md"))
    a = ap.parse_args()
    engine = get_engine()
    rows, passed = [], 0
    for line in open(Path(__file__).parent / "queries.jsonl"):
        q = json.loads(line)
        r = engine.ask(q["query"], model=q.get("model"), serial_number=q.get("serial_number"),
                       mode="llm" if a.llm else "extractive")
        checks = [r["status"] == q["expect_status"]]
        if q.get("expect_reason"):
            checks.append(r["reason_code"] in q["expect_reason"])
        if q.get("expect_applicability"):
            checks.append((r["applicability"] or {}).get("result") == q["expect_applicability"])
        cited = sorted({c["docket"] for s in r["statements"] for c in s["citations"]})
        if q.get("expect_docket"):
            checks.append(cited == [q["expect_docket"]])
        ok = all(checks)
        passed += ok
        rows.append(f"| {q['id']} | {q['category']} | {q['query'][:60]} | {r['status']} {r['reason_code'] or ''} | "
                    f"{(r['applicability'] or {}).get('result', '')} | {', '.join(cited)} | "
                    f"{len(r['removed_statements'])} | {r['latency_ms']} | {'PASS' if ok else '**FAIL**'} |")
    header = ("| # | Category | Query | Status | Applicability | Cited docket | Removed by verifier | Latency ms | Result |\n"
              "|---|---|---|---|---|---|---|---|---|")
    table = f"{header}\n" + "\n".join(rows) + f"\n\n**{passed}/{len(rows)} passed** (mode: {'llm' if a.llm else 'extractive'})\n"
    print(table)
    Path(a.out).write_text(table)


if __name__ == "__main__":
    main()
