"""Download/cache notice sources and extract text with honest location metadata.

No API key is sent to document hosts. PDF locations are physical PDF page numbers;
HTML and spreadsheet text carry paragraph/section IDs, not invented page numbers.
"""
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .text import clean


class SourceError(RuntimeError):
    pass


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class SourceFetcher:
    def __init__(self, cache_dir: str | Path, timeout: float = 30,
                 max_bytes: int = 20 * 1024 * 1024, refresh: bool = False,
                 session: requests.Session | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout, self.max_bytes, self.refresh = timeout, max_bytes, refresh
        self.session = session or requests.Session()
        if session is None:
            retries = Retry(total=2, backoff_factor=1, backoff_max=10,
                            status_forcelist=[429, 500, 502, 503, 504],
                            allowed_methods=["GET"], respect_retry_after_header=False)
            self.session.mount("https://", HTTPAdapter(max_retries=retries))
            self.session.mount("http://", HTTPAdapter(max_retries=retries))
        self.session.headers.update({"User-Agent": "AeroTrace/0.1 (public regulatory document research)"})

    def download(self, url: str) -> tuple[bytes, dict]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise SourceError("Source must be an HTTP(S) URL without credentials.")
        key = hashlib.sha256(url.encode()).hexdigest()
        body_path = self.cache_dir / f"{key}.body"
        meta_path = self.cache_dir / f"{key}.json"
        if not self.refresh and body_path.exists() and meta_path.exists():
            try:
                body = body_path.read_bytes()
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                if hashlib.sha256(body).hexdigest() == metadata["sha256"]:
                    return body, metadata
            except (ValueError, KeyError, OSError):
                pass  # Incomplete/corrupt cache entries are downloaded again.
        try:
            with self.session.get(url, timeout=(10, self.timeout), stream=True) as response:
                if response.status_code != 200:
                    raise SourceError(f"Source returned HTTP {response.status_code}.")
                parts, size = [], 0
                for part in response.iter_content(chunk_size=64 * 1024):
                    size += len(part)
                    if size > self.max_bytes:
                        raise SourceError(f"Source exceeds the {self.max_bytes}-byte download limit.")
                    parts.append(part)
                body = b"".join(parts)
                metadata = {"requested_url": url, "resolved_url": response.url,
                            "content_type": response.headers.get("Content-Type", ""),
                            "encoding": response.encoding,
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                            "sha256": hashlib.sha256(body).hexdigest()}
        except requests.RequestException:
            raise SourceError("Source download failed (network error or timeout).") from None
        temporary = body_path.with_suffix(".body.tmp")
        temporary.write_bytes(body)
        temporary.replace(body_path)
        _write_json(meta_path, metadata)
        return body, metadata

    def fetch(self, url: str) -> dict:
        """Resolve Federal Register notice URLs through their API where possible."""
        parsed = urlsplit(url)
        if parsed.hostname in {"federalregister.gov", "www.federalregister.gov"}:
            match = re.search(r"/(?:d/|documents/\d{4}/\d{2}/\d{2}/)(\d{4}-\d+)", parsed.path)
            if match:
                api_url = f"https://www.federalregister.gov/api/v1/documents/{match.group(1)}.json"
                try:
                    raw, _ = self.download(api_url)
                    metadata = json.loads(raw)
                    if not isinstance(metadata, dict):
                        metadata = {}
                except (SourceError, ValueError):
                    metadata = {}
                for candidate in dict.fromkeys([metadata.get("body_html_url"), metadata.get("pdf_url")]):
                    if not candidate:
                        continue
                    try:
                        result = self._extract_url(candidate, depth=0)
                    except SourceError:
                        continue
                    result.update({"source_url": url, "notice_url": metadata.get("html_url", url),
                                   "official_pdf_url": metadata.get("pdf_url"),
                                   "federal_register_document_number": metadata.get("document_number")})
                    return result
        result = self._extract_url(url, depth=0)
        result["source_url"] = url
        return result

    def _extract_url(self, url: str, depth: int) -> dict:
        body, metadata = self.download(url)
        if body.lstrip().startswith(b"%PDF-"):
            blocks = self._pdf_blocks(body)
            kind = "pdf"
        elif "html" in metadata["content_type"].lower() or body.lstrip().startswith(b"<"):
            text = body.decode(metadata.get("encoding") or "utf-8", errors="replace")
            blocks, links = self._html_blocks(text, metadata["resolved_url"])
            if links and depth < 1:
                for link in links[:3]:
                    try:
                        return self._extract_url(link, depth + 1)
                    except SourceError:
                        continue
            if not blocks:
                raise SourceError("No reliable notice text found (landing page, blocked page, or unsupported HTML).")
            kind = "html"
        else:
            raise SourceError("Unsupported source format; expected a PDF or HTML notice.")
        if not any(b["text"] and not b["low_confidence"] for b in blocks):
            raise SourceError("No extractable document text; a scanned PDF may require OCR.")
        return {"status": "downloaded", "format": kind, "resolved_url": metadata["resolved_url"],
                "fetched_at": metadata["fetched_at"], "raw_sha256": metadata["sha256"],
                "blocks": blocks, "text": "\n\n".join(b["text"] for b in blocks if b["text"])}

    @staticmethod
    def _pdf_blocks(body: bytes) -> list[dict]:
        try:
            reader = PdfReader(io.BytesIO(body))
            blocks = []
            for page_no, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                paragraphs = [clean(p) for p in re.split(r"\n\s*\n", text) if clean(p)]
                if not paragraphs:
                    blocks.append({"paragraph": f"pdf-p{page_no}", "section": "UNREADABLE PAGE",
                                   "page": page_no, "page_kind": "pdf_page", "text": "", "low_confidence": True})
                for i, paragraph in enumerate(paragraphs, start=1):
                    blocks.append({"paragraph": f"pdf-p{page_no}.{i}", "section": "", "page": page_no,
                                   "page_kind": "pdf_page", "text": paragraph, "low_confidence": False})
            return blocks
        except Exception:
            raise SourceError("PDF text extraction failed (invalid, encrypted, or unsupported PDF).") from None

    @staticmethod
    def _html_blocks(text: str, url: str) -> tuple[list[dict], list[str]]:
        soup = BeautifulSoup(text, "html.parser")
        # Identify the actual document before stripping navigation. Never ingest a submission form.
        root = soup.select_one(".document-content, #fulltext_content, .full-text, article")
        fragment = soup.body is None and soup.html is None
        if root is None and fragment:
            root = soup
        if root is None:
            root = soup.find("main")
        links = []
        for link in soup.find_all(["a", "link"], href=True):
            href = urljoin(url, link["href"])
            label = link.get_text(" ", strip=True).lower()
            if (link.get("type") == "application/pdf" or
                    re.search(r"(?:\.pdf)(?:$|\?)", href, re.I) and
                    any(word in label for word in ["pdf", "download", "document"])):
                links.append(href)
        if root is None or re.search(r"access denied|request access|verify you are human|captcha", soup.get_text(" ")[:1500], re.I):
            return [], list(dict.fromkeys(links))
        for element in root.select("script, style, nav, header, footer, form, noscript, aside, .fr-seal-block, .modal, .document-tools"):
            if element.parent is not None:
                element.decompose()
        section, blocks = "", []
        block_tags = {"p", "li", "table", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
        for element in root.find_all(list(block_tags)):
            if any(parent.name in block_tags for parent in element.parents if parent is not root):
                continue
            value = clean(element.get_text(" ", strip=True))
            if not value:
                continue
            if re.fullmatch(r"h[1-6]", element.name):
                section = value
            page = element.get("data-page", "")
            page = int(page) if page.isdigit() else None
            blocks.append({"paragraph": f"html-{len(blocks) + 1}", "source_element_id": element.get("id"),
                           "section": section, "page": page,
                           "page_kind": "federal_register_page" if page is not None else "html_paragraph",
                           "text": value, "low_confidence": False})
        # A navigation-only landing page must not masquerade as regulatory evidence.
        if sum(len(block["text"]) for block in blocks) < 200:
            return [], list(dict.fromkeys(links))
        # Prefer actual notice text. Only follow a document link if the page was a landing page.
        return blocks, []
