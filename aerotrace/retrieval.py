"""Persistent two-stage OpenRouter retrieval: question -> documents -> chunks.

Build once: python -m aerotrace.retrieval build
Search: python -m aerotrace.retrieval search "R22 teeter stop brackets" --top-n 20 --top-m 6
This returns evidence, not a generated answer or an applicability decision.
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import tiktoken

from .config import ROOT, load_env
from .embeddings import DEFAULT_MODEL, EmbeddingError, OpenRouterEmbeddings
from .ingest import load_tsv
from .text import norm_docket

DEFAULT_INPUT = ROOT / ".cache" / "aerotrace" / "prepared" / "documents.jsonl"
DEFAULT_INDEX = ROOT / ".cache" / "aerotrace" / "retrieval"
SCHEMA_VERSION = 1
DOCKET_RE = re.compile(r"\b(?:FAA-\d{4}-\d{3,5}(?:R\d|_S|-W)?|\d{2,4}-[A-Z]{2}-\d{1,3}-AD)\b", re.I)
RECORD_RE = re.compile(r"\bNPRM-\d+\b", re.I)


class RetrievalError(RuntimeError):
    """An unavailable, stale, or invalid retrieval index."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tokenizer():
    # tiktoken downloads its public vocabulary on first use. Keep that cache in the project.
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(ROOT / ".cache" / "aerotrace" / "tiktoken"))
    return tiktoken.get_encoding("cl100k_base")


def split_text(text: str, max_tokens: int = 500, overlap: int = 64) -> list[dict]:
    """Token windows within ONE source block; never combine documents/pages/sections.

    Move boundaries when necessary to preserve complete Unicode characters. Each
    text is an exact substring of the source; no truncation or replacement characters.
    """
    if not 8 <= max_tokens <= 6000 or not 0 <= overlap < max_tokens:
        raise ValueError("chunk_tokens must be 8..6000; overlap must be 0..chunk_tokens-1.")
    if not text.strip():
        return []
    encoding = tokenizer()
    tokens = encoding.encode_ordinary(text)
    pieces = encoding.decode_tokens_bytes(tokens)
    windows, start = [], 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        while end > start:
            try:
                value = b"".join(pieces[start:end]).decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if end <= start:
            raise RetrievalError("Cannot split source text at a valid character boundary.")
        if value.strip():
            windows.append({"text": value, "token_start": start, "token_end": end})
        if end == len(tokens):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end:
            try:
                b"".join(pieces[next_start:end]).decode("utf-8")
                break
            except UnicodeDecodeError:
                next_start += 1
        start = next_start
    return windows


def _context(doc: dict, text: str) -> str:
    return (f"{doc['title']}\nManufacturer: {doc.get('make', '')}\n"
            f"Models: {', '.join(doc.get('models', []))}\nDocket: {doc['docket']}\n\n{text}")


def make_corpus(records: list[dict], chunk_tokens: int = 500, overlap: int = 64) -> tuple[list[dict], list[dict]]:
    """Create summary documents and citable chunks from prepared source records."""
    docs, chunks, seen = [], [], set()
    for record in records:
        doc_id = record["doc_id"]
        if not doc_id or doc_id in seen:
            raise RetrievalError("Prepared records must have unique, nonempty doc_id values.")
        seen.add(doc_id)
        doc = {key: record.get(key) for key in (
            "doc_id", "record_id", "docket", "title", "summary", "make", "models", "revision",
            "publish_date", "fr_citation", "pages", "url", "dataset_revision", "source_record",
        )}
        source = record.get("source", {})
        doc["source_status"] = source.get("status", "missing")
        doc["summary_basis"] = "summary" if (doc.get("summary") or "").strip() else "metadata"
        # The single missing summary is represented by its supplied metadata, never invented text.
        summary = (doc.get("summary") or "").strip() or doc["title"]
        doc["embedding_text"] = _context(doc, summary)
        docs.append(doc)
        blocks = []
        if (doc.get("summary") or "").strip():
            blocks.append({"paragraph": "summary", "section": "Summary", "text": doc["summary"],
                           "page": None, "page_kind": "spreadsheet_summary", "evidence_kind": "summary"})
        metadata_text = (f"Docket: {doc['docket']}. Subject: {doc['title']}. "
                         f"Published: {doc.get('publish_date') or 'not provided'}. "
                         f"Revision label: {doc.get('revision') or 'not provided'}. "
                         f"Federal Register citation: {doc.get('fr_citation') or 'not provided'}.")
        blocks.append({"paragraph": "metadata", "section": "Record metadata", "text": metadata_text,
                       "page": None, "page_kind": "spreadsheet_record", "evidence_kind": "metadata"})
        blocks.extend({**block, "evidence_kind": "document"} for block in source.get("blocks", [])
                      if not block.get("low_confidence") and block.get("text", "").strip())
        for block_no, block in enumerate(blocks):
            for window_no, window in enumerate(split_text(block["text"], chunk_tokens, overlap)):
                chunk = {"chunk_id": f"{doc_id}:b{block_no}:c{window_no}", "doc_id": doc_id,
                         "paragraph": block["paragraph"], "section": block.get("section", ""),
                         "page": block.get("page"), "page_kind": block.get("page_kind"),
                         "source_element_id": block.get("source_element_id"),
                         "evidence_kind": block["evidence_kind"], **window,
                         "source_url": source.get("notice_url") or source.get("source_url") or doc.get("url"),
                         "resolved_url": source.get("resolved_url")}
                chunk["embedding_text"] = _context(doc, chunk["text"])
                chunks.append(chunk)
    if not docs or not chunks:
        raise RetrievalError("No documents or readable chunks were found. Run python -m aerotrace.prepare first.")
    return docs, chunks


def _normalise(vectors, expected_rows: int, expected_dimensions: int | None = None) -> np.ndarray:
    try:
        array = np.asarray(vectors, dtype=np.float32)
    except (TypeError, ValueError):
        raise RetrievalError("Embedding vectors have an invalid shape or type.") from None
    if (array.ndim != 2 or array.shape[0] != expected_rows or not array.shape[1]
            or expected_dimensions is not None and array.shape[1] != expected_dimensions
            or not np.isfinite(array).all()):
        raise RetrievalError("Embedding count, dimensions, or values are invalid.")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or np.any(norms == 0):
        raise RetrievalError("Embedding vectors must have finite, nonzero lengths.")
    return array / norms


class VectorCache:
    """Content-addressed float32 vectors, shared by builds and repeated questions.

    A fresh SQLite connection per operation supports Streamlit request threads.
    API keys and original input strings are never stored in this cache.
    """
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, dimensions INTEGER, data BLOB)")

    @staticmethod
    def _key(model, dimensions, text):
        return hashlib.sha256(json.dumps([model, dimensions, text], ensure_ascii=False).encode()).hexdigest()

    def embed(self, texts: list[str], client, progress=None) -> tuple[np.ndarray, dict]:
        keys = [self._key(client.model, client.dimensions, text) for text in texts]
        unique = dict(zip(keys, texts))
        found, missing = {}, []
        with sqlite3.connect(self.path, timeout=30) as db:
            for key in unique:
                row = db.execute("SELECT dimensions, data FROM vectors WHERE key = ?", (key,)).fetchone()
                if row:
                    vector = np.frombuffer(row[1], dtype=np.float32).copy()
                    if (len(vector) == row[0] and (client.dimensions is None or len(vector) == client.dimensions)
                            and np.isfinite(vector).all() and np.any(vector)):
                        found[key] = vector
                        continue
                missing.append(key)
        usage, completed = {}, len(found)
        if progress:
            progress(completed, len(unique), "cached")
        for start in range(0, len(missing), client.batch_size):
            batch_keys = missing[start:start + client.batch_size]
            raw = client.embed([unique[key] for key in batch_keys])
            # Validate before caching; a partial failed build remains resumable.
            _normalise(raw, len(batch_keys), client.dimensions)
            with sqlite3.connect(self.path, timeout=30) as db:
                for key, vector in zip(batch_keys, raw):
                    array = np.asarray(vector, dtype=np.float32)
                    db.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)", (key, len(array), array.tobytes()))
                    found[key] = array
            for key, value in client.last_usage.items():
                if type(value) in (int, float):
                    usage[key] = usage.get(key, 0) + value
            completed += len(batch_keys)
            if progress:
                progress(completed, len(unique), "embedded")
        matrix = _normalise([found[key] for key in keys], len(keys), client.dimensions)
        return matrix, {"total_inputs": len(texts), "unique_inputs": len(unique),
                        "cached_inputs": len(unique) - len(missing), "new_inputs": len(missing), "usage": usage}


def _client(model=None, dimensions=None, batch_size=32):
    client = OpenRouterEmbeddings(model=model, dimensions=dimensions, batch_size=batch_size)
    # Explicit dimensions make cache identity and query/document compatibility unambiguous.
    if client.dimensions is None:
        if client.model != DEFAULT_MODEL:
            raise RetrievalError("Set AEROTRACE_EMBEDDING_DIMENSIONS for this embedding model.")
        client.dimensions = 3072
    return client


def build_index(input_path: str | Path = DEFAULT_INPUT, index_dir: str | Path = DEFAULT_INDEX,
                chunk_tokens: int = 500, overlap: int = 64, client=None, progress=None) -> dict:
    input_path, index_dir = Path(input_path).resolve(), Path(index_dir)
    if not input_path.exists():
        raise RetrievalError("Prepared corpus is missing. Run python -m aerotrace.prepare first.")
    input_bytes = input_path.read_bytes()
    input_hash = hashlib.sha256(input_bytes).hexdigest()
    records = [json.loads(line) for line in input_bytes.decode("utf-8").splitlines() if line.strip()]
    source_files, source_records = {}, {}
    for record in records:
        filename = record.get("source_record", {}).get("path")
        if filename and filename not in source_files:
            path = Path(filename)
            if not path.is_file():
                raise RetrievalError("The dataset used to prepare these records is missing. Prepare the corpus again.")
            source_files[filename] = _digest(path)
            if path.suffix == ".tsv":
                source_records[filename] = {d["doc_id"]: d for d in load_tsv(path)[0]}
                revisions = path.with_name("revision_chains.tsv")
                if revisions.is_file():
                    source_files[str(revisions)] = _digest(revisions)
        if filename in source_records:
            current = source_records[filename].get(record["doc_id"], {})
            fields = ("title", "summary", "docket", "models", "url", "revision", "publish_date", "dataset_revision")
            if any(str(current.get(key)) != str(record.get(key)) for key in fields):
                raise RetrievalError("Prepared records do not match the current dataset. Run python -m aerotrace.prepare again.")
            if record.get("source", {}).get("status") == "embedded":
                actual = current.get("embedded_text", "").replace("\\n", "\n").replace("\\t", "\t")
                if actual != record["source"]["text"]:
                    raise RetrievalError("Embedded source text changed. Prepare the corpus again.")
    docs, chunks = make_corpus(records, chunk_tokens, overlap)
    texts = [d["embedding_text"] for d in docs] + [c["embedding_text"] for c in chunks]
    encoding = tokenizer()
    if any(len(encoding.encode_ordinary(text)) > 8191 for text in texts):
        raise RetrievalError("An embedding input exceeds 8,191 tokens; reduce its size before building.")
    client = client or _client()
    index_dir.mkdir(parents=True, exist_ok=True)
    matrix, cache_stats = VectorCache(index_dir / "vectors.sqlite3").embed(texts, client, progress)
    generation = uuid.uuid4().hex
    arrays_path = index_dir / f"vectors-{generation}.npz"
    corpus_path = index_dir / f"corpus-{generation}.json"
    np.savez(arrays_path, summaries=matrix[:len(docs)], chunks=matrix[len(docs):])
    corpus_path.write_text(json.dumps({"documents": docs, "chunks": chunks}, ensure_ascii=False), encoding="utf-8")
    if _digest(input_path) != input_hash or any(_digest(Path(p)) != digest for p, digest in source_files.items()):
        raise RetrievalError("Sources changed during the build. Prepare and rebuild before searching.")
    manifest = {
        "schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "model": client.model, "dimensions": matrix.shape[1], "chunk_tokens": chunk_tokens, "overlap": overlap,
        "prepared_path": str(input_path), "prepared_sha256": input_hash, "source_files": source_files,
        "documents": len(docs), "chunks": len(chunks),
        "source_status": dict(Counter(d["source_status"] for d in docs)),
        "evidence_kinds": dict(Counter(c["evidence_kind"] for c in chunks)),
        "arrays_file": arrays_path.name, "corpus_file": corpus_path.name, "embedding_cache": cache_stats,
    }
    # Publish only when the entire generation is ready; failures preserve the previous usable index.
    temporary = index_dir / f"manifest-{generation}.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(index_dir / "manifest.json")
    return manifest


class TwoStageIndex:
    def __init__(self, index_dir: str | Path = DEFAULT_INDEX, client=None):
        self.index_dir = Path(index_dir)
        try:
            self.manifest = json.loads((self.index_dir / "manifest.json").read_text(encoding="utf-8"))
            if self.manifest["schema_version"] != SCHEMA_VERSION:
                raise RetrievalError("Index format changed. Rebuild the semantic index.")
            self._check_sources()
            for field in ("corpus_file", "arrays_file"):
                if Path(self.manifest[field]).name != self.manifest[field]:
                    raise RetrievalError("Invalid index file path.")
            corpus = json.loads((self.index_dir / self.manifest["corpus_file"]).read_text(encoding="utf-8"))
            self.documents, self.chunks = corpus["documents"], corpus["chunks"]
            with np.load(self.index_dir / self.manifest["arrays_file"], allow_pickle=False) as arrays:
                self.summary_vectors = _normalise(arrays["summaries"], len(self.documents), self.manifest["dimensions"])
                self.chunk_vectors = _normalise(arrays["chunks"], len(self.chunks), self.manifest["dimensions"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RetrievalError("Semantic index is missing or invalid. Run python -m aerotrace.retrieval build.") from exc
        if len(self.documents) != self.manifest["documents"] or len(self.chunks) != self.manifest["chunks"]:
            raise RetrievalError("Index counts do not match its manifest. Rebuild the semantic index.")
        self.doc_by_id = {doc["doc_id"]: doc for doc in self.documents}
        if len(self.doc_by_id) != len(self.documents) or any(c["doc_id"] not in self.doc_by_id for c in self.chunks):
            raise RetrievalError("The index has duplicate documents or orphaned chunks.")
        self.cache = VectorCache(self.index_dir / "vectors.sqlite3")
        self.client = client  # Test/custom client only. Production requests each get their own HTTP session.

    def _check_sources(self):
        files = {self.manifest["prepared_path"]: self.manifest["prepared_sha256"], **self.manifest.get("source_files", {})}
        for filename, fingerprint in files.items():
            path = Path(filename)
            if not path.is_file() or _digest(path) != fingerprint:
                raise RetrievalError("The dataset or prepared sources changed. Prepare the corpus and rebuild the semantic index.")

    def search(self, query: str, top_n: int = 20, top_m: int = 6,
               allowed_doc_ids: set[str] | None = None) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Enter a nonempty question.")
        if type(top_n) is not int or top_n < 1 or type(top_m) is not int or top_m < 1:
            raise ValueError("top_n and top_m must be positive integers.")
        if len(tokenizer().encode_ordinary(query)) > 8191:
            raise ValueError("The question exceeds the embedding input limit (8,191 tokens).")
        started = time.perf_counter()
        eligible = [i for i, d in enumerate(self.documents) if allowed_doc_ids is None or d["doc_id"] in allowed_doc_ids]
        if not eligible:
            raise ValueError("No indexed documents match the document filter.")
        references = DOCKET_RE.findall(query) + RECORD_RE.findall(query)
        if references:
            matches = set()
            for reference in references:
                reference = reference.upper()
                scoped = {i for i in eligible if self.documents[i]["doc_id"].upper() == reference
                          or self.documents[i]["docket"].upper() == reference
                          or norm_docket(self.documents[i]["docket"]) == reference}
                if not scoped:
                    raise ValueError(f"No indexed documents match the explicit reference {reference}.")
                matches.update(scoped)
            eligible = [i for i in eligible if i in matches]
        client = self.client or _client(self.manifest["model"], self.manifest["dimensions"])
        if client.model != self.manifest["model"] or client.dimensions != self.manifest["dimensions"]:
            raise RetrievalError("Query embeddings must use the index's model and dimensions.")
        vector, stats = self.cache.embed([query], client)
        embedding_ms = (time.perf_counter() - started) * 1000
        question = vector[0]
        summary_scores = self.summary_vectors[eligible] @ question
        selected = [eligible[i] for i in np.argsort(-summary_scores, kind="stable")[:top_n]]
        documents = []
        for rank, i in enumerate(selected, start=1):
            score = float(self.summary_vectors[i] @ question)
            documents.append({k: v for k, v in self.documents[i].items() if k != "embedding_text"}
                             | {"rank": rank, "score": round(score, 6)})
        ranks = {d["doc_id"]: d["rank"] for d in documents}
        # Stage two is strictly restricted to the top-N documents; no global-chunk fallback.
        eligible_chunks = [i for i, chunk in enumerate(self.chunks) if chunk["doc_id"] in ranks]
        chunk_scores = self.chunk_vectors[eligible_chunks] @ question
        selected_chunks = [eligible_chunks[i] for i in np.argsort(-chunk_scores, kind="stable")[:top_m]]
        results = []
        for rank, i in enumerate(selected_chunks, start=1):
            chunk = self.chunks[i]
            doc = self.doc_by_id[chunk["doc_id"]]
            results.append({k: v for k, v in chunk.items() if k != "embedding_text"} | {
                "rank": rank, "score": round(float(self.chunk_vectors[i] @ question), 6),
                "document_rank": ranks[chunk["doc_id"]], "title": doc["title"], "docket": doc["docket"],
                "revision": doc.get("revision"), "publish_date": doc.get("publish_date"),
                "fr_citation": doc.get("fr_citation"), "page_range": doc.get("pages"),
            })
        elapsed = (time.perf_counter() - started) * 1000
        return {"query": query, "status": "RETRIEVED", "model": self.manifest["model"],
                "top_n": top_n, "top_m": top_m, "documents": documents, "chunks": results,
                "reference_filter": references,
                "corpus": {k: self.manifest[k] for k in ("documents", "chunks", "source_status", "evidence_kinds")},
                "query_embedding_cached": stats["new_inputs"] == 0, "embedding_usage": stats["usage"],
                "latency_ms": round(elapsed, 1), "embedding_ms": round(embedding_ms, 1),
                "ranking_ms": round(elapsed - embedding_ms, 1)}


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Prepare and persist document/chunk embeddings (paid API calls on cache misses).")
    build.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    build.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    build.add_argument("--chunk-tokens", type=int, default=500)
    build.add_argument("--overlap", type=int, default=64)
    build.add_argument("--batch-size", type=int, default=32)
    search = sub.add_parser("search", help="Embed a question and return both retrieval stages as JSON.")
    search.add_argument("query")
    search.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    search.add_argument("--top-n", type=int, default=20)
    search.add_argument("--top-m", type=int, default=6)
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = build_index(args.input, args.index_dir, args.chunk_tokens, args.overlap,
                                 client=_client(batch_size=args.batch_size),
                                 progress=lambda done, total, action: print(f"{action}: {done}/{total} unique texts", flush=True))
        else:
            result = TwoStageIndex(args.index_dir).search(args.query, args.top_n, args.top_m)
    except (EmbeddingError, RetrievalError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
