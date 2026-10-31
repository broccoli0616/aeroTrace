from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aerotrace.chunk import build_chunks
from aerotrace.config import settings
from aerotrace.embed import build_embeddings
from aerotrace.fetch import fetch_all
from aerotrace.workbook import import_workbook


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the AeroTrace corpus and hybrid-search index")
    parser.add_argument("--limit", type=int, help="Fetch only the first N notices (development only)")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-embed", action="store_true")
    args = parser.parse_args()
    settings.ensure_dirs()
    report = {"workbook": import_workbook(settings.workbook, settings.database)}
    if not args.skip_fetch:
        report["fetch"] = fetch_all(settings.database, settings.raw_dir, limit=args.limit)
    report["chunks"] = build_chunks(settings.database)
    if not args.skip_embed:
        report["embeddings"] = build_embeddings(settings)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

