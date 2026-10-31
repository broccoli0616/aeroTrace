"""Latency at scale: replicate the corpus N times (default 20x, roughly 35k passages) and time queries.
Usage: python eval/bench_latency.py [N]"""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aerotrace.engine import AeroTrace  # noqa: E402
from aerotrace.index import HybridIndex  # noqa: E402

n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
e = AeroTrace()
base = len(e.passages)
e.passages = [dict(p) for _ in range(n) for p in e.passages]
e.by_doc = {}
for i, p in enumerate(e.passages):
    e.by_doc.setdefault(p["doc_id"], []).append(i)
e.pidx = {p["passage_id"]: i for i, p in enumerate(e.passages)}
t = time.perf_counter()
e.index = HybridIndex(e.passages, e.docs)
print(f"indexed {len(e.passages)} passages ({n}x {base}) in {time.perf_counter() - t:.1f}s")
times = []
for line in open(Path(__file__).parent / "queries.jsonl"):
    q = json.loads(line)
    times.append(e.ask(q["query"], model=q.get("model"), serial_number=q.get("serial_number"))["latency_ms"])
print(f"queries: {len(times)}  median {statistics.median(times):.1f} ms  max {max(times):.1f} ms")
