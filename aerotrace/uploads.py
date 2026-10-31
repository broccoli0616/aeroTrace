"""User uploads: validate a file, parse it into documents + citable passages, and keep it on disk.

Accepted formats (anything else is rejected with a reason):
- .pdf   text PDFs; sections are found across page breaks, every passage keeps its page number
- .docx  Word; headings / lettered sections become sections, tables become one passage per row
- .xlsx  Excel; every sheet is read
- .tsv   tab-separated text
Spreadsheets in the FAA AD NPRM layout are loaded exactly like data/ad_nprm.tsv. Any other table becomes one
passage per row ("Column: value. Column: value.").

Uploads are not verified against the Federal Register, so every uploaded document is labelled as user-provided.
"""
import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import date, datetime
from itertools import groupby
from pathlib import Path

from .ingest import (_MAKE_ALIASES, _SECTION_RE, FAA_COLUMNS, REV_CORRECTION, REV_EXTENSION, REV_ORIGINAL,
                     REV_SUPPLEMENTAL, REV_WITHDRAWAL, _metadata_text, _passage, _revision_label, load_rows,
                     normalize_make)
from .rules import _models_from_text
from .text import clean, norm_docket

ALLOWED = {".pdf": "PDF", ".docx": "Word", ".xlsx": "Excel", ".tsv": "TSV"}
_HINTS = {".doc": "save it as .docx", ".xls": "save it as .xlsx", ".csv": "save it as .xlsx or tab-separated .tsv",
          ".txt": "save it as .docx or .pdf", ".pptx": "export it as .pdf"}
MAX_BYTES = 50 * 1024 * 1024
REVISION_CHOICES = [REV_ORIGINAL, REV_SUPPLEMENTAL, REV_CORRECTION, REV_WITHDRAWAL, REV_EXTENSION, "Unknown revision"]

_DOCKET_RE = re.compile(r"\bFAA-\d{4}-\d{3,6}(?:R\d+|_S|-W)?\b", re.I)
_AD_RE = re.compile(r"\b(?:AD|Airworthiness Directive(?: \(AD\))?)\s+(?:No\.\s*)?(\d{4}-\d{2}-\d{2})\b")
_TITLE_RE = re.compile(r"Airworthiness Directives?;\s*[^\n]{3,120}")
_DUE_RE = re.compile(r"on or before\s+([A-Z][a-z]+ \d{1,2}, \d{4})")


class UploadError(ValueError):
    """The file is rejected; the message is safe to show to the user."""


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(filename: str, data: bytes) -> str:
    """Return the file kind or raise UploadError. Checks the extension *and* the content, so a renamed file
    (e.g. an .exe called report.pdf) is rejected."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED:
        hint = f" Please {_HINTS[ext]}." if ext in _HINTS else ""
        raise UploadError(f"'{ext or 'no extension'}' files are not supported. "
                          f"Accepted: PDF, Word (.docx), Excel (.xlsx), TSV.{hint}")
    if not data:
        raise UploadError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise UploadError(f"The file is {len(data) / 1e6:.0f} MB; the limit is {MAX_BYTES // 2**20} MB.")
    if ext == ".pdf" and not data[:1024].lstrip().startswith(b"%PDF-"):
        raise UploadError("This file is named .pdf but is not a PDF.")
    if ext in (".docx", ".xlsx"):
        inner = "word/document.xml" if ext == ".docx" else "xl/workbook.xml"
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            raise UploadError(f"This file is named {ext} but is not a valid {ALLOWED[ext]} file "
                              f"(it may be password-protected or an old-format file renamed).") from None
        if inner not in names:
            raise UploadError(f"This file is named {ext} but is not a {ALLOWED[ext]} document.")
    if ext == ".tsv":
        if b"\x00" in data[:8192]:
            raise UploadError("This file is named .tsv but contains binary data.")
        if b"\t" not in data[:65536]:
            raise UploadError("This .tsv file has no tab characters; columns must be tab-separated.")
    return ALLOWED[ext]


def parse(filename: str, data: bytes) -> dict:
    """Validate and parse one upload. Returns {kind, sha256, docs, passages, warnings}."""
    kind = validate(filename, data)
    sha = file_hash(data)
    upload = {"file": filename, "sha256": sha, "kind": kind}
    source_kind = f"Uploaded {kind} (user-provided, not verified against the Federal Register)"
    prefix = f"upload/{Path(filename).stem}/"
    warnings = []
    try:
        if kind == "PDF":
            docs, passages, warnings = _parse_pdf(filename, data, prefix, upload, source_kind)
        elif kind == "Word":
            docs, passages = _parse_docx(filename, data, prefix, upload, source_kind)
        else:
            tables = _read_tsv(data) if kind == "TSV" else _read_xlsx(data)
            docs, passages = [], []
            for sheet, header, rows in tables:
                sheet_prefix = prefix if len(tables) == 1 else f"{prefix}{sheet}/"
                if FAA_COLUMNS <= set(header):
                    d, p = load_rows((dict(zip(header, r)) for _, r in rows), sheet_prefix,
                                     {"upload": upload, "source_kind": source_kind})
                else:
                    d, p = _generic_table(filename, sheet, header, rows, sheet_prefix, upload, source_kind)
                docs += d
                passages += p
    except UploadError:
        raise
    except Exception as e:  # corrupt / encrypted / unusual files: report, don't crash the app
        raise UploadError(f"Could not read this {kind} file ({type(e).__name__}: {e}).") from None
    if all(p["low_confidence"] for p in passages if p["paragraph"] != "metadata"):
        raise UploadError("No readable text was found." + (" Scanned PDFs need OCR, which is not supported yet."
                                                            if kind == "PDF" else " The file appears to be empty."))
    return {"kind": kind, "sha256": sha, "docs": docs, "passages": passages, "warnings": warnings}


# ---------- free text (PDF, Word) ----------

def _new_doc(doc_id: str, filename: str, text: str, upload: dict, source_kind: str) -> dict:
    """A document record with metadata guessed from the text. Anything not found stays empty;
    the review screen lets the user correct it before the document is added."""
    head = text[:20000]
    docket = (_DOCKET_RE.search(head) or [""])[0].upper()
    ad = _AD_RE.search(head)
    title = _TITLE_RE.search(head)
    low = head[:5000].lower()
    make_raw = next((key for key, _ in _MAKE_ALIASES if re.search(rf"\b{re.escape(key)}\b", low)), "")
    applic = re.search(r"(?:\(c\)\s*Applicability|Applicability:|This AD applies to)(.{0,1200})", text, re.S)
    models = list(dict.fromkeys(_models_from_text(clean(applic.group(0) if applic else head[:5000]))))
    summary = re.search(r"SUMMARY:\s*(.{20,2000}?)(?=\n\s*[A-Z]{4,}[A-Z ]*:|\Z)", head, re.S)
    due = _DUE_RE.search(head)
    try:
        comments_due = datetime.strptime(due.group(1), "%B %d, %Y").date() if due else None
    except ValueError:
        comments_due = None
    summary_text = clean(summary.group(1)) if summary else ""
    return {
        "doc_id": doc_id, "row": None, "docket": docket or Path(filename).stem,
        "docket_group": norm_docket(docket or doc_id),
        "revision": _revision_label(docket, "", summary_text or head[:2000]) if docket else "Unknown revision",
        "title": clean(title.group(0)) if title else Path(filename).stem,
        "make": normalize_make(make_raw) if make_raw else "", "make_raw": normalize_make(make_raw) if make_raw else "",
        "models": models, "product_type": "", "product_subtype": "", "cfr_part": "",
        "ad_number": ad.group(1) if ad else "", "issue_date": None, "publish_date": date.today(),
        "comments_due": comments_due, "fr_citation": "", "pages": "", "url": "", "summary": summary_text,
        "has_fulltext": True, "comments": "", "withdrawn": False, "source_kind": source_kind, "upload": upload,
    }


def _sections(segments: list[tuple[object, str]]) -> list[tuple[str, str, str, object]]:
    """Split text that comes in (page, text) segments into (paragraph_id, section, text, page).
    Lettered FAA sections such as '(c) Applicability' are kept whole even across page breaks;
    otherwise text is chunked into 200-1500 character passages. The page is where the passage starts."""
    full, starts = "", []
    for page, text in segments:
        starts.append((len(full), page))
        full += text + "\n"

    def page_at(pos):
        return next((pg for off, pg in reversed(starts) if off <= pos), starts[0][1] if starts else None)

    heads = list(_SECTION_RE.finditer(full))
    out = []
    if len(heads) >= 3:
        pre = _flat(full[: heads[0].start()])
        if len(pre) > 40:
            out += _chunks(pre, "Preamble", page_at(0), "preamble")
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(full)
            body = _flat(full[h.end(): end]).lstrip(": ")
            if body:
                out += _chunks(body, h.group(2).strip(), page_at(h.start()), f"({h.group(1)})")
        return out
    for page, text in segments:  # no lettered sections: sentence-aligned chunks, page by page
        out += _chunks(_flat(text), "", page, f"p{page}", max_len=800)
    return out


def _flat(text: str) -> str:
    """Line breaks inside a PDF section are layout, not meaning: join them so phrases like
    'serial numbers 0100 / through 0199' stay intact for search, rules and quoting."""
    return re.sub(r"\s*\n\s*", " ", clean(text))


def _chunks(text: str, section: str, page, pid: str, max_len: int = 1500) -> list[tuple[str, str, str, object]]:
    """Long text is split at sentence boundaries so each passage stays citable."""
    if len(text) <= max_len:
        return [(pid, section, text, page)]
    parts, buf = [], ""
    for sent in re.split(r"(?<=[.;])\s+", text):
        if buf and len(buf) + len(sent) > max_len:
            parts.append(buf)
            buf = ""
        buf = f"{buf} {sent}".strip()
    if buf:
        parts.append(buf)
    return [(f"{pid}.{i}", section, p, page) for i, p in enumerate(parts, start=1)]


def _parse_pdf(filename, data, prefix, upload, source_kind):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise UploadError("This PDF is password-protected.") from None
    segments, unreadable = [], []
    for page_no, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if len(text.strip()) < 20:
            unreadable.append(page_no)
        else:
            segments.append((page_no, text))
    doc = _new_doc(prefix.rstrip("/"), filename, "\n".join(t for _, t in segments), upload, source_kind)
    doc["pages"] = f"{len(reader.pages)} pages"
    passages = [{**_passage(doc, pid, sec, text), "page": page} for pid, sec, text, page in _sections(segments)]
    passages.insert(0, _passage(doc, "metadata", "Record metadata", _metadata_text(doc)))
    warnings = [f"Page {n} has no extractable text (scanned image?) and was skipped." for n in unreadable]
    return [doc], passages, warnings


def _parse_docx(filename, data, prefix, upload, source_kind):
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    body_text, paras, tables = [], [], []
    section = ""
    for block in d.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(block, d)
            text = para.text.strip()
            if not text:
                continue
            body_text.append(text)
            if (para.style.name or "").lower().startswith(("heading", "title")):
                section = text
            else:
                paras.append((section, text))
        elif tag == "tbl":
            rows = [[c.text.strip() for c in r.cells] for r in Table(block, d).rows]
            if rows:
                tables.append((section, rows))
                body_text += [" ".join(r) for r in rows]
    doc = _new_doc(prefix.rstrip("/"), filename, "\n".join(body_text), upload, source_kind)
    passages = [_passage(doc, "metadata", "Record metadata", _metadata_text(doc))]
    # Word has no fixed pages, so the citation "page" is the section heading.
    text = "\n".join(t for _, t in paras)
    if len(_SECTION_RE.findall(text)) >= 3:
        for pid, sec, t, _ in _sections([("", text)]):
            passages.append({**_passage(doc, pid, sec, t), "page": sec})
    else:
        for n, (sec, group) in enumerate(groupby(paras, key=lambda x: x[0]), start=1):
            joined = clean(" ".join(t for _, t in group))
            for pid, s_, chunk, _ in _chunks(joined, sec, None, f"para-{n}", max_len=800):
                passages.append({**_passage(doc, pid, s_, chunk), "page": sec or "body"})
    for t_no, (sec, rows) in enumerate(tables, start=1):
        header = rows[0]
        for r_no, row in enumerate(rows[1:], start=1):
            line = ". ".join(f"{h}: {v}" for h, v in zip(header, row) if v) + "."
            if len(line) > 1:
                passages.append({**_passage(doc, f"table{t_no}-row{r_no}", sec or f"Table {t_no}", line),
                                 "page": f"table {t_no}, row {r_no}", "low_confidence": False})
    return [doc], passages


# ---------- tables (TSV, Excel) ----------

def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%m/%d/%Y")
    if isinstance(v, date):
        return v.strftime("%m/%d/%Y")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _read_tsv(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    csv.field_size_limit(10**9)
    return [t for t in [_table("Sheet1", list(csv.reader(io.StringIO(text), delimiter="\t")))] if t]


def _read_xlsx(data: bytes):
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    tables = []
    for ws in wb.worksheets:
        t = _table(ws.title, [[_cell(v) for v in r] for r in ws.iter_rows(values_only=True)])
        if t:
            tables.append(t)
    wb.close()
    return tables


def _table(sheet: str, rows: list[list[str]]):
    """(sheet, header, [(row_number, cells)]) with the first non-empty row as header; blank rows dropped
    but the spreadsheet's own row numbers kept for citations."""
    numbered = [(n, [c.strip() for c in r]) for n, r in enumerate(rows, start=1) if any(c.strip() for c in r)]
    if not numbered:
        return None
    return sheet, numbered[0][1], numbered[1:]


def _generic_table(filename, sheet, header, rows, prefix, upload, source_kind):
    lines = [(n, ". ".join(f"{h}: {v}" for h, v in zip(header, r) if h and v) + ".") for n, r in rows]
    doc = _new_doc(prefix.rstrip("/"), filename, "\n".join(t for _, t in lines), upload, source_kind)
    doc["title"] = f"{Path(filename).stem} / {sheet}"
    # A 'Model' column is more reliable than guessing from text.
    model_cols = [i for i, h in enumerate(header) if h.strip().lower() in ("model", "models", "aircraft model")]
    if model_cols:
        doc["models"] = list(dict.fromkeys(
            m.strip() for _, r in rows for i in model_cols if i < len(r) for m in re.split(r"[|,;]", r[i]) if m.strip()))
    passages = [_passage(doc, "metadata", "Record metadata", _metadata_text(doc))]
    for n, line in lines:
        if len(line) > 1:
            passages.append({**_passage(doc, f"row-{n}", sheet, line), "page": f"{sheet} row {n}",
                             "low_confidence": False})  # a short row is still a complete record
    return [doc], passages


# ---------- review overrides ----------

def apply_overrides(docs: list[dict], passages: list[dict], overrides: dict) -> tuple[list[dict], list[dict]]:
    """Apply fields the user corrected on the review screen: {doc_id: {title, docket, make, models, revision}}."""
    by_id = {d["doc_id"]: d for d in docs}
    for doc_id, o in (overrides or {}).items():
        d = by_id.get(doc_id)
        if not d:
            continue
        if o.get("title"):
            d["title"] = o["title"]
        if o.get("docket"):
            d["docket"] = o["docket"].strip().upper()
            d["docket_group"] = norm_docket(d["docket"])
        if "make" in o:
            d["make"] = d["make_raw"] = o["make"].strip()
        if "models" in o:
            d["models"] = [m.strip() for m in o["models"] if m.strip()]
        if o.get("revision"):
            d["revision"] = o["revision"]
            d["withdrawn"] = o["revision"] == REV_WITHDRAWAL
    for p in passages:  # keep the metadata passage in sync with the corrected fields
        if p["paragraph"] == "metadata" and p["doc_id"] in overrides:
            p["text"] = _metadata_text(by_id[p["doc_id"]])
    return docs, passages


# ---------- storage ----------

class UploadStore:
    """Uploaded files live in one folder as <sha16>__<name>, with <sha16>.json holding the original name and the
    user's review overrides. Re-uploading the same bytes is detected by hash and never parsed twice."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    def _files(self):
        return sorted(self.folder.glob("*__*")) if self.folder.exists() else []

    def find(self, sha: str) -> Path | None:
        return next((f for f in self._files() if f.name.startswith(sha[:16] + "__")), None)

    def save(self, filename: str, data: bytes, overrides: dict | None = None) -> Path:
        sha = file_hash(data)
        if existing := self.find(sha):
            return existing
        self.folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[:120]
        path = self.folder / f"{sha[:16]}__{safe}"
        path.write_bytes(data)
        meta = {"file": Path(filename).name, "sha256": sha, "uploaded": datetime.now().isoformat(timespec="seconds"),
                "overrides": overrides or {}}
        (self.folder / f"{sha[:16]}.json").write_text(json.dumps(meta, indent=2))
        return path

    def delete(self, sha: str) -> bool:
        path = self.find(sha)
        if not path:
            return False
        path.unlink()
        (self.folder / f"{sha[:16]}.json").unlink(missing_ok=True)
        return True

    def items(self) -> list[dict]:
        out = []
        for f in self._files():
            meta_path = self.folder / f"{f.name.split('__', 1)[0]}.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            out.append({"path": f, "file": meta.get("file", f.name.split("__", 1)[1]),
                        "sha256": meta.get("sha256", ""), "uploaded": meta.get("uploaded", ""),
                        "overrides": meta.get("overrides", {})})
        return out

    def load_all(self) -> tuple[list[dict], list[dict], list[str]]:
        """Parse every stored upload (with its overrides). Files that no longer parse are reported, not fatal."""
        docs, passages, errors = [], [], []
        for item in self.items():
            try:
                r = parse(item["file"], item["path"].read_bytes())
            except UploadError as e:
                errors.append(f"{item['file']}: {e}")
                continue
            d, p = apply_overrides(r["docs"], r["passages"], item["overrides"])
            docs += d
            passages += p
        return docs, passages, errors
