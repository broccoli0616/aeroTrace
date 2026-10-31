from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

from .embed import VectorIndex
from .text import tokens


STOP = {
    "a", "an", "and", "are", "does", "for", "how", "in", "is", "it", "of", "on", "or", "the",
    "this", "to", "what", "which", "with", "proposal", "propose", "proposes", "proposed", "directive", "ad", "faa",
}


def _fts_query(question: str) -> str:
    topical = re.sub(r"\bFAA-\d{4}-\d{3,6}(?:R\d+)?\b", " ", question, flags=re.I)
    topical = re.sub(r"\b(?:AD\s*)?(?:19|20)\d{2}-\d{2}-\d{2}\b", " ", topical, flags=re.I)
    terms = [t for t in tokens(topical) if t not in STOP and len(t) > 1]
    safe = [f'"{t.replace(chr(34), "")}"' for t in terms[:24]]
    return " OR ".join(safe)


def _allowed_chunks(con: sqlite3.Connection, parsed: dict) -> set[str] | None:
    clauses, params = [], []
    if parsed.get("record_id"):
        clauses.append("n.record_id=?")
        params.append(parsed["record_id"])
    if parsed.get("docket"):
        if not parsed.get("record_id"):
            clauses.append("UPPER(n.docket_number)=UPPER(?)")
            params.append(parsed["docket"])
    if parsed.get("ad_number"):
        clauses.append("n.ad_number=?")
        params.append(parsed["ad_number"])
    if parsed.get("model") and not clauses:
        wanted = re.sub(r"[\s-]+", "-", parsed["model"].upper()).strip("-")
        matching_records = []
        for row in con.execute("SELECT record_id,model FROM notices WHERE model IS NOT NULL AND model<>''"):
            listed = [re.sub(r"[\s-]+", "-", p.upper()).strip("-") for p in row["model"].split("|")]
            if any(wanted == item or wanted.startswith(item + "-") for item in listed):
                matching_records.append(row["record_id"])
        if matching_records:
            placeholders = ",".join("?" for _ in matching_records)
            clauses.append(f"n.record_id IN ({placeholders})")
            params.extend(matching_records)
    if not clauses:
        return None
    rows = con.execute(
        f"SELECT c.chunk_id FROM chunks c JOIN notices n ON n.record_id=c.record_id WHERE {' OR '.join(clauses)}",
        params,
    ).fetchall()
    return {row[0] for row in rows}


def keyword_search(con: sqlite3.Connection, question: str, allowed: set[str] | None, k: int = 100) -> list[tuple[str, float]]:
    match = _fts_query(question)
    if not match:
        return [(chunk_id, 1.0) for chunk_id in sorted(allowed or set())[:k]]
    rows = con.execute(
        "SELECT chunk_id,bm25(chunks_fts) AS score FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
        (match, max(k * 4, 200) if allowed is not None else k),
    ).fetchall()
    out = [(row["chunk_id"], float(-row["score"])) for row in rows if allowed is None or row["chunk_id"] in allowed]
    if not out and allowed is not None:
        return [(chunk_id, 1.0) for chunk_id in sorted(allowed)[:k]]
    return out[:k]


def hybrid_search(
    con: sqlite3.Connection,
    vectors: VectorIndex,
    question: str,
    parsed: dict,
    *,
    k: int = 40,
    keyword_only: bool = False,
) -> dict:
    allowed = _allowed_chunks(con, parsed)
    if allowed is not None and not allowed:
        return {"hits": [], "documents": [], "reason": "EXPLICIT_DOCUMENT_NOT_FOUND"}
    keyword = keyword_search(con, question, allowed, 100)
    semantic = [] if keyword_only else vectors.search(question, allowed, 100)
    rrf = defaultdict(float)
    for rank, (chunk_id, _) in enumerate(keyword, start=1):
        rrf[chunk_id] += 1 / (60 + rank)
    for rank, (chunk_id, _) in enumerate(semantic, start=1):
        rrf[chunk_id] += 1 / (60 + rank)
    if allowed is not None:
        for chunk_id in allowed:
            if chunk_id in rrf:
                rrf[chunk_id] += 0.02
    ranked = sorted(rrf, key=rrf.get, reverse=True)[:k]
    if not ranked:
        return {"hits": [], "documents": [], "reason": "NO_MATCH"}
    placeholders = ",".join("?" for _ in ranked)
    rows = con.execute(
        f"""SELECT c.*,n.docket_number,n.subject,n.publish_date,n.make,n.model,n.citation,n.pages,
        n.official_action,n.fetch_status
        FROM chunks c JOIN notices n ON n.record_id=c.record_id
        WHERE c.chunk_id IN ({placeholders})""",
        ranked,
    ).fetchall()
    by_id = {row["chunk_id"]: dict(row) for row in rows}
    hits = [{**by_id[cid], "rrf_score": rrf[cid]} for cid in ranked if cid in by_id]
    doc_scores = defaultdict(float)
    for hit in hits:
        doc_scores[hit["record_id"]] = max(doc_scores[hit["record_id"]], hit["rrf_score"])
    documents = sorted(doc_scores, key=doc_scores.get, reverse=True)
    return {"hits": hits, "documents": documents, "reason": None,
            "keyword_hits": len(keyword), "semantic_hits": len(semantic),
            "semantic_max": max((score for _, score in semantic), default=None)}


def evidence_for_document(con: sqlite3.Connection, search: dict, record_id: str, max_chunks: int = 7) -> list[dict]:
    selected = [h for h in search["hits"] if h["record_id"] == record_id][:max_chunks]
    applicability = [dict(row) for row in con.execute(
        """SELECT c.*,n.docket_number,n.subject,n.publish_date,n.citation,n.pages,n.official_action
        FROM chunks c JOIN notices n ON n.record_id=c.record_id
        WHERE c.record_id=? AND LOWER(c.section) LIKE '%applicability%' ORDER BY c.ordinal""",
        (record_id,),
    ).fetchall()]
    seen = {item["chunk_id"] for item in selected}
    for item in applicability:
        if item["chunk_id"] not in seen:
            selected.append(item)
            seen.add(item["chunk_id"])
    return selected[:max_chunks + 3]

