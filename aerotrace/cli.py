"""Command line: python -m aerotrace.cli "Does the R22 AD apply to serial number 1200?" [--model R22] [--llm]"""
import argparse
import json

from .config import ROOT, load_env
from .engine import AeroTrace, get_engine


def main():
    load_env()
    ap = argparse.ArgumentParser(description="AeroTrace: traceable search over FAA proposed ADs")
    ap.add_argument("query")
    ap.add_argument("--model")
    ap.add_argument("--serial")
    ap.add_argument("--part")
    ap.add_argument("--llm", action="store_true", help="let Claude write the answer (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--full", action="store_true", help="include full source passages in the JSON")
    ap.add_argument("--retrieval", choices=["bm25", "semantic"], default="bm25")
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--top-m", type=int, default=6)
    a = ap.parse_args()
    try:
        if a.retrieval == "semantic":
            from .retrieval import TwoStageIndex

            engine = AeroTrace(ROOT / "data" / "ad_nprm.tsv", ROOT / "data" / "pdfs", semantic_index=TwoStageIndex())
        else:
            engine = get_engine()
        res = engine.ask(a.query, model=a.model, serial_number=a.serial, part_number=a.part,
                         mode="llm" if a.llm else "extractive", top_n=a.top_n, top_m=a.top_m)
    except (ValueError, RuntimeError) as exc:
        ap.exit(1, f"{exc}\n")
    if not a.full:
        for st in res["statements"]:
            for c in st["citations"]:
                c["source_text"] = c["source_text"][:200] + "..."
        if res["applicability"]:
            res["applicability"].pop("rule_text", None)
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
