from __future__ import annotations

import json
import re
from pathlib import Path

from .schema import connect
from .text import clean, digest


PAGE_RE = re.compile(r"\[\[Page\s+(\d+)\]\]", re.I)
SECTION_RE = re.compile(
    r"(?m)^\(([a-z]{1,2})\)\s+([A-Z][^\n]{1,100})$"
)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def _split_long(text: str, max_chars: int = 2200) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = SENTENCE_RE.split(text)
    out, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > max_chars:
            out.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def structure_chunks(text: str, default_page: str | None = None) -> list[dict]:
    heads = list(SECTION_RE.finditer(text))
    spans = []
    if heads:
        preamble = clean(text[:heads[0].start()])
        if preamble:
            spans.append(("Preamble", "preamble", preamble, 0))
        for i, head in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            body = clean(text[head.end():end])
            spans.append((f"({head.group(1)}) {head.group(2).strip()}", f"({head.group(1)})", body, head.start()))
    else:
        spans = [("Document text", "body", text, 0)]
    result = []
    for section, paragraph, body, offset in spans:
        page_hits = list(PAGE_RE.finditer(text[:offset]))
        page = page_hits[-1].group(1) if page_hits else default_page
        body = PAGE_RE.sub("", body)
        blocks = [clean(b) for b in re.split(r"\n\s*\n", body) if clean(b)]
        if not blocks:
            blocks = [body]
        merged, buffer = [], ""
        for block in blocks:
            candidate = f"{buffer}\n\n{block}".strip()
            if buffer and len(candidate) > 2200:
                merged.extend(_split_long(buffer))
                buffer = block
            else:
                buffer = candidate
        if buffer:
            merged.extend(_split_long(buffer))
        for part in merged:
            if len(part) >= 20:
                result.append({"page": page, "section": section, "paragraph": paragraph, "text": part})
    return result


def build_chunks(database: Path) -> dict:
    con = connect(database)
    rows = con.execute("SELECT * FROM notices WHERE fetch_status='fetched' ORDER BY record_id").fetchall()
    inserted = 0
    with con:
        con.execute("DELETE FROM chunks")
        for row in rows:
            payload = json.loads(Path(row["raw_path"]).read_text(encoding="utf-8"))
            page_match = re.search(r"\d+", str(row["pages"] or ""))
            parts = structure_chunks(payload["text"], page_match.group(0) if page_match else None)
            for ordinal, part in enumerate(parts):
                chunk_id = f"{row['record_id']}:{ordinal:04d}"
                searchable = " ".join(
                    filter(None, [row["subject"], row["docket_number"], row["make"], row["model"],
                                  part["section"], part["text"]])
                )
                con.execute(
                    """INSERT INTO chunks
                    (chunk_id,record_id,source_kind,page,section,paragraph,ordinal,text,
                     searchable_text,text_hash,source_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (chunk_id, row["record_id"], "official_full_text", part["page"], part["section"],
                     part["paragraph"], ordinal, part["text"], searchable, digest(searchable), row["source_url"]),
                )
                inserted += 1
    con.close()
    return {"documents": len(rows), "chunks": inserted}

