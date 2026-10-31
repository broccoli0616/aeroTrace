from __future__ import annotations

import re


def resolve(con, question: str, parsed: dict, requested_record: str = "") -> dict:
    """Resolve a requested notice version without treating superseded ADs as docket revisions."""
    record_match = re.search(r"\bNPRM-\d{3}\b", question, re.I)
    record = requested_record or (record_match.group().upper() if record_match else "")
    row = con.execute("SELECT record_id,docket_number FROM notices WHERE UPPER(record_id)=UPPER(?)", (record,)).fetchone() if record else None
    if record and not row:
        return {"error": "EXPLICIT_DOCUMENT_NOT_FOUND", "reason": "The requested record is not in this corpus."}
    docket = parsed.get("docket") or (row["docket_number"] if row else None)
    if row and docket.upper() != row["docket_number"].upper():
        return {"error": "CONFLICTING_VERSION", "reason": "The record ID and docket refer to different notices."}
    if not docket:
        return {"docket": None, "requested_record": record}
    rows = [dict(r) for r in con.execute(
        "SELECT record_id,docket_number,publish_date,official_action,source_url,fetch_status FROM notices WHERE UPPER(docket_number)=UPPER(?) ORDER BY publish_date,record_id", (docket,)
    )]
    if not rows:
        return {"error": "EXPLICIT_DOCUMENT_NOT_FOUND", "reason": "The requested docket is not in this corpus."}
    latest_date = max(r["publish_date"] or "" for r in rows)
    latest = [r for r in rows if (r["publish_date"] or "") == latest_date]
    version_match = re.search(r"\bversion\s+(\d+)\b", question, re.I)
    dates = re.findall(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", question)
    dates = [d for d in dates if d not in (docket or "")]
    candidates = rows
    if record:
        candidates = [r for r in candidates if r["record_id"].upper() == record.upper()]
    if version_match:
        n = int(version_match.group(1))
        candidates = [r for r in candidates if rows.index(r) + 1 == n]
    if dates:
        candidates = [r for r in candidates if r["publish_date"] in dates]
    if not candidates:
        return {"error": "CONFLICTING_VERSION", "reason": "The requested record, version, and publication date do not identify the same notice."}
    explicit = bool(record or version_match or dates)
    selected = candidates if explicit else latest
    if len(latest) != 1 and not explicit:
        return {"error": "AMBIGUOUS_VERSION", "reason": "Multiple records share the latest publication date. Provide a record ID."}
    if len(selected) != 1:
        return {"error": "AMBIGUOUS_VERSION", "reason": "More than one notice matches the requested publication date. Provide a record ID."}
    chosen = selected[0]
    for i, item in enumerate(rows, 1):
        item["version"] = i
        item["versions_total"] = len(rows)
        item["corpus_status"] = "latest" if item["publish_date"] == latest_date else "earlier"
    return {"docket": docket, "requested_record": chosen["record_id"], "history": rows,
            "revision": {"version_count": len(rows), "selected_record": chosen["record_id"],
                         "selected_version": rows.index(chosen) + 1, "selected_date": chosen["publish_date"],
                         "latest_record": latest[0]["record_id"] if len(latest) == 1 else None,
                         "latest_version": rows.index(latest[0]) + 1 if len(latest) == 1 else None,
                         "latest_date": latest_date, "is_latest": chosen["publish_date"] == latest_date,
                         "selection_reason": "explicit" if explicit else "latest_in_corpus"}}
