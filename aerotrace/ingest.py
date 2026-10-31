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
_URL_RE = re.compile(r"https?://\S+")

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
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


REV_ORIGINAL, REV_SUPPLEMENTAL, REV_CORRECTION = "Original NPRM", "Supplemental NPRM (revised proposal)", "OFR correction"
REV_WITHDRAWAL, REV_EXTENSION = "Withdrawal", "Comment period extension/reopening"


def _revision_label(docket: str, comments: str, summary: str) -> str:
    d, s = docket.strip().upper(), summary.lower()
    if re.search(r"R\d+$", d):
        return f"{REV_CORRECTION} {re.search(r'(R\d+)$', d).group(1)}"
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
    # Fallback: split on line breaks / wide gaps, then merge tiny chunks so each passage is 200-1500 chars.
    chunks = [clean(c) for c in re.split(r"\n|\s{3,}", raw) if clean(c)]
    buf, n = "", 1
    for c in chunks:
        buf = f"{buf} {c}".strip()
        if len(buf) >= 200:
            out.append((f"para-{n}", "", buf[:1500]))
            buf, n = "", n + 1
    if buf:
        out.append((f"para-{n}", "", buf))
    return out


FAA_COLUMNS = {"Docket Number", "Subject Heading", "Summary", "Make", "Model", "Page Number", "Citation",
               "Citation Publish Date", "Issue Date", "Comments Due Date", "AD Number", "Comments",
               "Link for Submitting Public Comment", "Product Type", "Product Subtype", "CFR Part Reference"}


def load_tsv(path: str | Path) -> tuple[list[dict], list[dict]]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return load_rows(csv.DictReader(f, delimiter="\t"))


def load_rows(rows, id_prefix: str = "", extra: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Rows in the FAA AD NPRM spreadsheet layout (from the TSV or an uploaded TSV / Excel sheet)."""
    docs, passages, seen = [], [], set()
    for row_no, row in enumerate(rows, start=1):
        row = {k: ("" if v is None else str(v)) for k, v in row.items() if k}
        docket = row["Docket Number"].strip()
        pages = row["Page Number"].strip().strip("()")
        m = re.search(r"\d+", pages)
        first_page = m.group() if m else f"row{row_no}"
        link_field = row["Link for Submitting Public Comment"].strip()
        fulltext = "" if link_field.startswith("http") else link_field
        url = link_field if link_field.startswith("http") else (_URL_RE.search(link_field) or [None])[0]
        comments = row["Comments"].strip()
        summary = clean(row["Summary"])
        doc_id = f"{id_prefix}{docket}:p{first_page}"
        if doc_id in seen:  # two rows can share docket + page (e.g. an NPRM and its withdrawal)
            doc_id = f"{doc_id}-row{row_no}"
        seen.add(doc_id)
        doc = {
            "doc_id": doc_id,
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
            "comments": comments,
            "withdrawn": _revision_label(docket, comments, summary) == REV_WITHDRAWAL,
            "source_kind": "FAA AD NPRM (proposed rule, not legally binding)",
            **(extra or {}),
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
