"""Prepare source documents without creating embeddings or changing retrieval.

Offline: python -m aerotrace.prepare
Download sources too: python -m aerotrace.prepare --fetch --limit 10
"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from .config import ROOT, load_env
from .ingest import _split_fulltext, load_tsv
from .sources import SourceError, SourceFetcher


def prepare_document(doc: dict, fetcher: SourceFetcher | None = None) -> dict:
    record = dict(doc)
    embedded = record.pop("embedded_text", "")
    if embedded:
        blocks = [{"paragraph": pid, "section": title, "page": None, "page_kind": "spreadsheet_section",
                   "text": text, "low_confidence": False} for pid, title, text in _split_fulltext(embedded)]
        source = {"status": "embedded", "format": "spreadsheet_text", "source_url": doc["url"],
                  "raw_sha256": hashlib.sha256(embedded.encode()).hexdigest(),
                  "text": embedded.replace("\\n", "\n").replace("\\t", "\t"), "blocks": blocks}
    elif doc["url"] and fetcher is not None:
        try:
            source = fetcher.fetch(doc["url"])
        except SourceError as exc:
            source = {"status": "unavailable", "source_url": doc["url"], "error": str(exc), "text": "", "blocks": []}
    else:
        source = {"status": "not_fetched" if doc["url"] else "missing", "source_url": doc["url"],
                  "text": "", "blocks": []}
    record["has_fulltext"] = bool(source["text"])
    record["source"] = source
    # The spreadsheet's page range is retained in record['pages']; it is not an exact block page.
    return record


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data" / "ad_nprm.tsv")
    parser.add_argument("--output", type=Path, default=ROOT / ".cache" / "aerotrace" / "prepared" / "documents.jsonl")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".cache" / "aerotrace" / "sources")
    parser.add_argument("--fetch", action="store_true", help="Download notices for rows containing URLs.")
    parser.add_argument("--refresh", action="store_true", help="Redownload sources instead of using the raw cache.")
    parser.add_argument("--limit", type=int, help="Process only the first N matching records.")
    parser.add_argument("--docket", action="append", help="Only prepare this docket; may be repeated.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.refresh and not args.fetch:
        parser.error("--refresh requires --fetch")
    docs, _ = load_tsv(args.input)
    if args.docket:
        docs = [doc for doc in docs if doc["docket"] in args.docket]
    if args.limit:
        docs = docs[:args.limit]
    if not docs:
        parser.error("No matching source records.")
    fetcher = SourceFetcher(args.cache_dir, refresh=args.refresh) if args.fetch else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    counts = Counter()
    with temporary.open("w", encoding="utf-8") as stream:
        for i, doc in enumerate(docs, start=1):
            record = prepare_document(doc, fetcher)
            record["source_record"] = {"path": str(args.input.resolve()), "data_row": doc["row"],
                                       "column": doc["source_column"]}
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            status = record["source"]["status"]
            counts[status] += 1
            detail = f" ({record['source']['error']})" if status == "unavailable" else ""
            print(f"[{i}/{len(docs)}] {doc['doc_id']}: {status}{detail}", flush=True)
    temporary.replace(args.output)
    print(json.dumps({"documents": len(docs), "source_status": dict(counts), "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
