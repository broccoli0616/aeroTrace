"""Load the FAA AD NPRM spreadsheet (TSV) and optional PDFs into documents + citable passages.

A *document* is one row of the TSV (one Federal Register notice).
A *passage* is the smallest citable unit: the summary, one lettered section such as "(c) Applicability",
or one paragraph of the full regulatory text. Every passage carries the metadata needed for a citation.
"""
import csv
import re
from datetime import date, datetime
from pathlib import Path

from .text import clean, norm_docket

csv.field_size_limit(10**9)

# Lettered section headers in the full proposed-AD text, e.g. "(c) Applicability" followed by a line break / big gap.
_SECTION_RE = re.compile(
    r"(?:^|\n|\s{2,})\(([a-z]{1,2})\)\s+([A-Z][A-Za-z0-9 ,/&'-]{2,70}?)(?=\s*\n|\s{2,}|:)"
)


def split_source_field(value: str) -> tuple[str, str | None]:
    """Return (embedded regulatory text, notice URL) from the mixed source-link field.

    Labels such as 'Withdrawal: https://...' are links, not regulatory text.
    Manufacturer/service-bulletin URLs inside long text are not notice citations.
    """
    value = value.strip()
    normalized = value.replace("\\n", "\n").replace("\\t", "\t")
    matches = re.findall(r"https?://[^\s<>\"']+", normalized)
    urls = [url.rstrip(".,;)]}") for url in matches]
    if not urls:
        return value, None
    notice_url = next((u for u in urls if re.match(r"https?://(?:www\.)?federalregister\.gov/", u)), None)
    remainder = re.sub(r"https?://[^\s<>\"']+", "", normalized).strip(" \t\r\n\"':;.,()-")
    if len(remainder) < 120 and len(remainder.split()) <= 8:
        return "", notice_url or urls[0]
    return value, notice_url

_MAKE_ALIASES = [
    ("airbus helicopters", "Airbus Helicopters"), ("eurocopter", "Airbus Helicopters"),
    ("airbus canada", "Airbus Canada"), ("airbus", "Airbus"),
    ("boeing", "Boeing"), ("mcdonnell douglas", "McDonnell Douglas"),
    ("bombardier", "Bombardier"), ("embraer", "Embraer"), ("rolls-royce", "Rolls-Royce"),
    ("bell", "Bell"), ("gulfstream", "Gulfstream"), ("dassault", "Dassault"),
    ("sikorsky", "Sikorsky"), ("general electric", "General Electric"), ("piper", "Piper"),
    ("cessna", "Cessna"), ("textron aviation", "Textron Aviation"), ("leonardo", "Leonardo"),
    ("agusta", "Leonardo"), ("pratt & whitney", "Pratt & Whitney"), ("cfm", "CFM International"),
    ("robinson", "Robinson"), ("honeywell", "Honeywell"), ("grob", "Grob"), ("bae systems", "BAE Systems"),
    ("fokker", "Fokker"), ("mitsubishi", "Mitsubishi"), ("lockheed", "Lockheed"),
    ("md helicopters", "MD Helicopters"), ("safran", "Safran"), ("de havilland", "De Havilland"),
]


def normalize_make(make: str) -> str:
    """'The Boeing Company' and 'Boeing' -> 'Boeing'. Unknown makers keep their name minus legal suffixes."""
    low = make.lower()
    for key, canon in _MAKE_ALIASES:
        if key in low:
            return canon
    return re.sub(r"(,?\s+(Inc\.?|Ltd\.?|LLC|S\.A\.?|SAS|GmbH|Company|Corporation|plc))+$", "", make.strip())


def _date(s: str):
    for format in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), format).date()
        except ValueError:
            pass
    return None


REV_ORIGINAL, REV_SUPPLEMENTAL, REV_CORRECTION = "Original NPRM", "Supplemental NPRM (revised proposal)", "OFR correction"
REV_WITHDRAWAL, REV_EXTENSION = "Withdrawal", "Comment period extension/reopening"


def _revision_label(docket: str, comments: str, summary: str) -> str:
    d, s = docket.strip().upper(), summary.lower()
    if correction := re.search(r"R\d+$", d):
        return f"{REV_CORRECTION} {correction.group()}"
    if d.endswith("-W") or re.search(r"\bwithdraw(s|ing|n)?\b", s[:300]) or "withdraws" in comments.lower():
        return REV_WITHDRAWAL
    if d.endswith("_S") or "revising a notice of proposed rulemaking" in s or "supplemental notice" in s:
        return REV_SUPPLEMENTAL
    if re.search(r"(extension|reopening) of the comment period", s):
        return REV_EXTENSION
    return REV_ORIGINAL


def _split_fulltext(raw: str) -> list[tuple[str, str, str]]:
    """Return (paragraph_id, section_title, text). Uses lettered sections when present, else paragraph chunks."""
    raw = raw.replace("\\n", "\n").replace("\\t", "  ")
    heads = list(_SECTION_RE.finditer(raw))
    out = []
    if len(heads) >= 3:
        preamble = clean(raw[: heads[0].start()])
        if len(preamble) > 40:
            out.append(("preamble", "Preamble", preamble))
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
            body = clean(raw[h.end(): end]).lstrip(": ")
            if body:
                out.append((f"({h.group(1)})", h.group(2).strip(), body))
        return out
    # Fallback: split on line breaks / wide gaps, then merge tiny chunks without dropping text.
    chunks = [clean(c) for c in re.split(r"\n|\s{3,}", raw) if clean(c)]
    buf, n = "", 1
    for c in chunks:
        buf = f"{buf} {c}".strip()
        if len(buf) >= 200:
            out.append((f"para-{n}", "", buf))
            buf, n = "", n + 1
    if buf:
        out.append((f"para-{n}", "", buf))
    return out


def load_tsv(path: str | Path) -> tuple[list[dict], list[dict]]:
    """Load the current named-column index; retain support for the original export."""
    docs, passages, seen = [], [], set()
    revision_rows = {}
    revision_path = Path(path).with_name("revision_chains.tsv")
    if revision_path.exists():
        with revision_path.open(encoding="utf-8", newline="") as stream:
            revision_rows = {r["record_id"]: r for r in csv.DictReader(stream, delimiter="\t")}
    with open(path, encoding="utf-8", errors="replace") as f:
        for row_no, row in enumerate(csv.DictReader(f, delimiter="\t"), start=1):
            record_id = row.get("record_id", "").strip()
            current_schema = "docket_number" in row
            original = row
            if current_schema:
                row = {
                    "Docket Number": original["docket_number"], "Subject Heading": original["subject"],
                    "Issue Date": original["issue_date"], "Citation Publish Date": original["publish_date"],
                    "Make": original["make"], "Model": original["model"],
                    "Product Type": original["product_type"], "Product Subtype": original["product_subtype"],
                    "AD Number": original["ad_number"], "Citation": original["citation"],
                    "Page Number": original["pages"], "Link for Submitting Public Comment": original["federal_register_link"],
                    "Summary": original["summary"], "Comments": "", "Comments Due Date": "", "CFR Part Reference": "",
                }
            docket = row["Docket Number"].strip()
            pages = row["Page Number"].strip().strip("()")
            m = re.search(r"\d+", pages)
            first_page = m.group() if m else f"row{row_no}"
            link_field = row["Link for Submitting Public Comment"].strip()
            fulltext, url = split_source_field(link_field)
            comments = row["Comments"].strip()
            summary = clean(row["Summary"])
            doc_id = record_id or f"{docket}:p{first_page}"
            if doc_id in seen:  # two rows can share docket + page (e.g. an NPRM and its withdrawal)
                if record_id:
                    raise ValueError(f"Duplicate record_id: {record_id}")
                doc_id = f"{doc_id}-row{row_no}"
            seen.add(doc_id)
            doc = {
                "doc_id": doc_id,
                "record_id": record_id or None,
                "row": row_no,
                "docket": docket,
                "docket_group": norm_docket(docket),
                "revision": _revision_label(docket, comments, summary),
                "title": row["Subject Heading"].strip(),
                "make": normalize_make(row["Make"]) if row["Make"].strip() else "",
                "make_raw": row["Make"].strip(),
                "models": [m.strip() for m in row["Model"].split("|") if m.strip()],
                "product_type": row["Product Type"].strip(),
                "product_subtype": row["Product Subtype"].strip(),
                "cfr_part": row["CFR Part Reference"].strip(),
                "ad_number": row["AD Number"].strip(),
                "issue_date": _date(row["Issue Date"]),
                "publish_date": _date(row["Citation Publish Date"]),
                "comments_due": _date(row["Comments Due Date"]),
                "fr_citation": row["Citation"].strip(),
                "pages": pages,
                "url": url,
                "summary": summary,
                "has_fulltext": bool(fulltext),
                "embedded_text": fulltext,
                "source_field_kind": "embedded_text" if fulltext else "url" if url else "missing",
                "source_column": "federal_register_link" if current_schema else "Link for Submitting Public Comment",
                "supersedes_ad": original.get("supersedes_ad", ""),
                "mentions_serial_numbers": original.get("mentions_serial_numbers", "").lower() == "yes" if current_schema else None,
                "dataset_revision": revision_rows.get(record_id),
                "comments": comments,
                "withdrawn": _revision_label(docket, comments, summary) == REV_WITHDRAWAL,
                "source_kind": "FAA AD NPRM (proposed rule, not legally binding)",
            }
            docs.append(doc)
            passages.append(_passage(doc, "metadata", "Record metadata", _metadata_text(doc)))
            if summary:
                passages.append(_passage(doc, "summary", "Summary", summary))
            if fulltext:
                for pid, title, text in _split_fulltext(fulltext):
                    passages.append(_passage(doc, pid, title, text))
    return docs, passages


def _metadata_text(doc: dict) -> str:
    """The spreadsheet's structured fields as a citable passage, so 'when is the comment due date?' can be answered."""
    parts = [f"Docket number: {doc['docket']}.", f"Subject: {doc['title']}.", f"Notice type: {doc['revision']}."]
    if doc["issue_date"]:
        parts.append(f"Issue date: {doc['issue_date']:%B %d, %Y}.")
    if doc["publish_date"]:
        parts.append(f"Published in the Federal Register: {doc['publish_date']:%B %d, %Y}, {doc['fr_citation']}, {doc['pages']}.")
    if doc["comments_due"]:
        parts.append(f"Comments due date: {doc['comments_due']:%B %d, %Y}.")
    if doc["make_raw"]:
        parts.append(f"Make: {doc['make_raw']}.")
    if doc["models"]:
        parts.append(f"Models: {', '.join(doc['models'])}.")
    if doc["product_type"]:
        parts.append(f"Product type: {doc['product_type']} {doc['product_subtype']}.".replace(" .", "."))
    if doc["ad_number"]:
        parts.append(f"AD number: {doc['ad_number']}.")
    if doc["comments"]:
        parts.append(f"Note: {doc['comments']}.")
    return " ".join(parts)


def _passage(doc: dict, paragraph: str, section: str, text: str) -> dict:
    return {
        "passage_id": f"{doc['doc_id']}#{paragraph}",
        "doc_id": doc["doc_id"],
        "paragraph": paragraph,
        "section": section,
        "page": doc["pages"],
        "text": text,
        "low_confidence": len(text) < 40,
    }


def load_pdfs(folder: str | Path) -> tuple[list[dict], list[dict]]:
    """Optional: ingest extra PDFs (e.g. final ADs) from data/pdfs/. One passage per paragraph per page.

    Name files like '2026-21-03_revA.pdf' so the document ID and revision are recorded.
    Pages with no extractable text are flagged (scanned pages are not OCR-guessed).
    """
    folder = Path(folder)
    if not folder.exists():
        return [], []
    from pypdf import PdfReader

    docs, passages = [], []
    for pdf in sorted(folder.glob("*.pdf")):
        stem = pdf.stem
        doc_name, _, rev = stem.partition("_rev")
        doc = {
            "doc_id": stem, "row": None, "docket": doc_name, "docket_group": norm_docket(doc_name),
            "revision": f"Rev {rev}" if rev else "Unknown revision", "title": stem, "make": "", "make_raw": "",
            "models": [], "product_type": "", "product_subtype": "", "cfr_part": "", "ad_number": doc_name,
            "issue_date": None, "publish_date": date.fromtimestamp(pdf.stat().st_mtime), "comments_due": None,
            "fr_citation": "", "pages": "", "url": str(pdf), "summary": "", "has_fulltext": True,
            "comments": "", "withdrawn": False, "source_kind": "PDF",
        }
        docs.append(doc)
        for page_no, page in enumerate(PdfReader(pdf).pages, start=1):
            text = page.extract_text() or ""
            paras = [clean(p) for p in re.split(r"\n\s*\n", text) if clean(p)]
            if not paras:
                passages.append({**_passage(doc, f"p{page_no}", "UNREADABLE PAGE", "(no extractable text)"),
                                 "page": page_no, "low_confidence": True})
            for i, p in enumerate(paras, start=1):
                passages.append({**_passage(doc, f"p{page_no}.{i}", "", p), "page": page_no})
    return docs, passages
