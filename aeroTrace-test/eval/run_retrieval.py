from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aerotrace.config import settings
from aerotrace.embed import VectorIndex
from aerotrace.query import parse_query
from aerotrace.retrieve import hybrid_search
from aerotrace.schema import connect


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * p))]


def run(split: str, keyword_only: bool) -> dict:
    cases = [json.loads(line) for line in (Path(__file__).parent / "queries.jsonl").read_text().splitlines() if line]
    cases = [case for case in cases if split == "all" or case["split"] == split]
    con = connect(settings.database)
    vectors = VectorIndex(settings)
    rows, latencies = [], []
    for case in cases:
        parsed = parse_query(con, case["query"])
        started = time.perf_counter()
        result = hybrid_search(con, vectors, case["query"], parsed, keyword_only=keyword_only)
        latency = (time.perf_counter() - started) * 1000
        latencies.append(latency)
        top5 = result["documents"][:5]
        rows.append({"id": case["id"], "target": case["target"], "top5": top5,
                     "hit": case["target"] in top5, "latency_ms": round(latency, 1)})
    con.close()
    return {
        "split": split, "mode": "keyword" if keyword_only else "hybrid", "cases": len(rows),
        "top5_recall": sum(row["hit"] for row in rows) / max(1, len(rows)),
        "p50_ms": round(statistics.median(latencies), 1), "p95_ms": round(percentile(latencies, .95), 1),
        "results": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "heldout", "all"], default="heldout")
    parser.add_argument("--keyword-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.split, args.keyword_only), indent=2))

