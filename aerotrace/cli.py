"""Command line: python -m aerotrace.cli "Does the R22 AD apply to serial number 1200?" [--model R22] [--llm]"""
import argparse
import json

from .engine import get_engine


def main():
    ap = argparse.ArgumentParser(description="AeroTrace: traceable search over FAA proposed ADs")
    ap.add_argument("query")
    ap.add_argument("--model")
    ap.add_argument("--serial")
    ap.add_argument("--part")
    ap.add_argument("--llm", action="store_true", help="let Claude write the answer (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--full", action="store_true", help="include full source passages in the JSON")
    a = ap.parse_args()
    res = get_engine().ask(a.query, model=a.model, serial_number=a.serial, part_number=a.part,
                           mode="llm" if a.llm else "extractive")
    if not a.full:
        for st in res["statements"]:
            for c in st["citations"]:
                c["source_text"] = c["source_text"][:200] + "..."
        if res["applicability"]:
            res["applicability"].pop("rule_text", None)
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()
