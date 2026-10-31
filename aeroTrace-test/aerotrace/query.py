from __future__ import annotations

import re
import sqlite3


DOCKET_RE = re.compile(r"\b(?:FAA-\d{4}-\d{3,6}(?:R\d+)?|\d{2,4}-[A-Z]{2}-\d{2,3}-AD)\b", re.I)
AD_RE = re.compile(r"\b(?:AD\s*)?((?:19|20)\d{2}-\d{2}-\d{2})\b", re.I)
SERIAL_RE = re.compile(r"\b(?:serial(?:\s+number)?|S/N|MSN)\s*[:#]?\s*([A-Z0-9][A-Z0-9./-]*)", re.I)
PART_RE = re.compile(r"\b(?:part(?:\s+number)?|P/N)\s*[:#]?\s*([A-Z0-9][A-Z0-9./-]*)", re.I)


def parse_query(con: sqlite3.Connection, question: str, overrides: dict | None = None) -> dict:
    overrides = overrides or {}
    docket = next(iter(DOCKET_RE.findall(question)), None)
    ad_match = AD_RE.search(question)
    serial_match = SERIAL_RE.search(question)
    part_match = PART_RE.search(question)
    models = [r[0] for r in con.execute("SELECT DISTINCT model FROM notices WHERE model IS NOT NULL AND model<>''")]
    candidates = []
    for cell in models:
        candidates.extend(piece.strip() for piece in str(cell).split("|") if piece.strip())
    model = None
    upper = question.upper()
    for candidate in sorted(set(candidates), key=len, reverse=True):
        if re.search(rf"(?<![A-Z0-9]){re.escape(candidate.upper())}(?![A-Z0-9])", upper):
            model = candidate
            break
    parsed = {
        "docket": docket,
        "ad_number": ad_match.group(1) if ad_match else None,
        "model": model,
        "serial_number": serial_match.group(1) if serial_match else None,
        "part_number": part_match.group(1) if part_match else None,
    }
    for key, value in overrides.items():
        if value and str(value).strip():
            parsed[key] = str(value).strip()
    return parsed

