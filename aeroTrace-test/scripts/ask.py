from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aerotrace.engine import AeroTrace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--model", default="")
    parser.add_argument("--serial", default="")
    parser.add_argument("--docket", default="")
    parser.add_argument("--keyword-only", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    engine = AeroTrace()
    try:
        result = engine.ask(args.question, model=args.model, serial_number=args.serial, docket=args.docket,
                            keyword_only=args.keyword_only, use_llm=not args.no_llm)
        print(json.dumps(result, indent=2, default=str))
    finally:
        engine.close()


if __name__ == "__main__":
    main()

