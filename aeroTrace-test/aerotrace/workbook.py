from __future__ import annotations

from pathlib import Path

import openpyxl

from .schema import connect


NOTICE_COLUMNS = (
    "record_id", "docket_number", "subject", "issue_date", "publish_date", "make", "model",
    "product_type", "product_subtype", "ad_number", "supersedes_ad", "mentions_serial_numbers",
    "citation", "pages", "federal_register_link", "summary",
)


def _sheet_records(sheet, header_row: int) -> list[dict]:
    rows = list(sheet.values)
    header = [str(v or "").strip() for v in rows[header_row - 1]]
    return [dict(zip(header, row)) for row in rows[header_row:] if any(v is not None for v in row)]


def import_workbook(workbook: Path, database: Path) -> dict:
    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    notices = _sheet_records(wb["nprm_index"], 3)
    revisions = _sheet_records(wb["revision_chains"], 3)
    notes = _sheet_records(wb["data_notes"], 1)
    con = connect(database)
    with con:
        for row in notices:
            con.execute(
                """INSERT INTO notices (
                record_id,docket_number,subject,issue_date,publish_date,make,model,product_type,
                product_subtype,ad_number,supersedes_ad,mentions_serial_numbers,citation,pages,
                source_url,summary
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(record_id) DO UPDATE SET
                  docket_number=excluded.docket_number, subject=excluded.subject,
                  issue_date=excluded.issue_date, publish_date=excluded.publish_date,
                  make=excluded.make, model=excluded.model, product_type=excluded.product_type,
                  product_subtype=excluded.product_subtype, ad_number=excluded.ad_number,
                  supersedes_ad=excluded.supersedes_ad,
                  mentions_serial_numbers=excluded.mentions_serial_numbers,
                  citation=excluded.citation, pages=excluded.pages,
                  source_url=excluded.source_url, summary=excluded.summary""",
                tuple(row.get(c) for c in NOTICE_COLUMNS),
            )
        con.execute("DELETE FROM revision_chain")
        con.executemany(
            """INSERT INTO revision_chain
            (docket_number,version,versions_total,corpus_status,record_id,publish_date,subject)
            VALUES (?,?,?,?,?,?,?)""",
            [(r["docket_number"], r["version"], r["versions_total"], r["status"],
              r["record_id"], r["publish_date"], r["subject"]) for r in revisions],
        )
        con.execute("DELETE FROM data_notes")
        con.executemany(
            "INSERT INTO data_notes(topic,detail) VALUES (?,?)",
            [(r["topic"], r["detail"]) for r in notes],
        )
    con.close()
    return {"notices": len(notices), "revision_rows": len(revisions), "notes": len(notes)}

