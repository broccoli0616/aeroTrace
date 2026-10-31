from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
from bs4.element import NavigableString

from .schema import connect
from .text import clean, digest


DOCUMENT_RE = re.compile(
    r"https?://(?:www\.)?federalregister\.gov/documents/\d{4}/\d{2}/\d{2}/([0-9A-Za-z-]+)(?:/[^\s\"']*)?"
)
SHORT_RE = re.compile(r"https?://(?:www\.)?federalregister\.gov/d/([0-9A-Za-z-]+)")


def document_number(value: str | None) -> str | None:
    value = str(value or "")
    match = DOCUMENT_RE.search(value) or SHORT_RE.search(value)
    return match.group(1) if match else None


def _resolve(client: httpx.Client, docket: str, publish_date: str | None) -> str | None:
    def search(term: str) -> list[dict]:
        response = client.get(
            "https://www.federalregister.gov/api/v1/documents.json",
            params={"per_page": 50, "conditions[term]": term},
        )
        response.raise_for_status()
        return response.json().get("results", [])

    results = search(docket)
    if not results and docket.upper().endswith("-W"):
        results = search(docket[:-2])
    # Search-list responses usually omit docket_ids even when the detailed document has
    # them. Since the query itself is the exact docket, disambiguate proposal/final-rule
    # results by the supplied publication date. Older workbook rows can be one day behind
    # the printed Federal Register date, so accept a narrow three-day window.
    exact = [r for r in results if docket.upper() in " ".join(r.get("docket_ids") or []).upper()]
    candidates = exact or results
    if publish_date:
        expected = date.fromisoformat(publish_date)
        dated = [r for r in candidates if r.get("publication_date") and
                 abs((date.fromisoformat(r["publication_date"]) - expected).days) <= 3]
        if dated:
            dated.sort(key=lambda r: abs((date.fromisoformat(r["publication_date"]) - expected).days))
            return dated[0].get("document_number")
    return candidates[0].get("document_number") if len(candidates) == 1 else None


def _text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script,style,nav,footer,header"):
        tag.decompose()
    for marker in soup.select("span[id^='page-']"):
        page = str(marker.get("id", "")).removeprefix("page-")
        if page.isdigit():
            marker.insert_before(NavigableString(f"\n\n[[Page {page}]]\n\n"))
    # This endpoint often returns an HTML fragment with preamble, supplemental-info and
    # end-matter as top-level siblings. BeautifulSoup may synthesize a tiny <body> around
    # only one sibling, so extracting from the complete soup is the cross-year-safe path.
    main = soup.select_one("#fulltext_content_area") or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return clean(text)


def _fetch_one(client: httpx.Client, row: dict, raw_dir: Path) -> tuple[str, dict]:
    path = raw_dir / f"{row['record_id']}.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        one_page_notice = len(re.findall(r"\d+", str(row.get("pages") or ""))) == 1
        if len(cached.get("text", "")) >= 600 and ("[[Page " in cached.get("text", "") or one_page_notice):
            return "cached", cached
    doc_no = document_number(row.get("source_url"))
    if not doc_no:
        doc_no = _resolve(client, row["docket_number"], row.get("publish_date"))
    if not doc_no:
        return "unresolved", {"error": "No unique official Federal Register document found"}
    meta_response = client.get(f"https://www.federalregister.gov/api/v1/documents/{quote(doc_no)}.json")
    meta_response.raise_for_status()
    meta = meta_response.json()
    html_url = meta.get("body_html_url") or meta.get("html_url")
    html_response = client.get(html_url)
    html_response.raise_for_status()
    text = _text_from_html(html_response.text)
    if len(text) < 300:
        raise ValueError("Official page returned too little extractable text")
    payload = {
        "record_id": row["record_id"], "docket_number": row["docket_number"],
        "document_number": doc_no, "action": meta.get("action"),
        "publication_date": meta.get("publication_date"), "citation": meta.get("citation"),
        "html_url": meta.get("html_url") or html_url, "body_html_url": meta.get("body_html_url"),
        "pdf_url": meta.get("pdf_url"), "text": text,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return "fetched", payload


def fetch_all(database: Path, raw_dir: Path, *, limit: int | None = None, workers: int = 6) -> dict:
    raw_dir.mkdir(parents=True, exist_ok=True)
    con = connect(database)
    rows = [dict(row) for row in con.execute("SELECT * FROM notices ORDER BY record_id").fetchall()]
    if limit:
        rows = rows[:limit]
    counts = {"fetched": 0, "cached": 0, "unresolved": 0, "failed": 0}
    headers = {"User-Agent": "AeroTrace hackathon prototype (source-grounded research)"}
    with httpx.Client(timeout=45, follow_redirects=True, headers=headers) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_one, client, row, raw_dir): row for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                path = raw_dir / f"{row['record_id']}.json"
                try:
                    status, payload = future.result()
                    if status == "unresolved":
                        with con:
                            con.execute("UPDATE notices SET fetch_status='unresolved',fetch_error=? WHERE record_id=?",
                                        (payload["error"], row["record_id"]))
                        counts["unresolved"] += 1
                        continue
                    with con:
                        con.execute(
                            """UPDATE notices SET fetch_status='fetched',fetch_error=NULL,
                            official_document_number=?,official_action=?,source_url=?,raw_path=?,text_hash=?
                            WHERE record_id=?""",
                            (payload["document_number"], payload.get("action"), payload["html_url"],
                             str(path), digest(payload["text"]), row["record_id"]),
                        )
                    counts[status] += 1
                except Exception as exc:
                    with con:
                        con.execute("UPDATE notices SET fetch_status='failed',fetch_error=? WHERE record_id=?",
                                    (f"{type(exc).__name__}: {str(exc)[:300]}", row["record_id"]))
                    counts["failed"] += 1
    con.close()
    return counts

