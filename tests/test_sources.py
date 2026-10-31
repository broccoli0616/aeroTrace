import io
import json
from datetime import date
from unittest.mock import Mock

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from aerotrace.ingest import _split_fulltext, load_tsv, split_source_field
from aerotrace.prepare import prepare_document
from aerotrace.sources import SourceError, SourceFetcher


NOTICE = "<article><h2 id='applicability'>Applicability</h2><p id='p-1'>" + (
    "This proposed directive applies to Model R22 helicopters with the specified serial numbers. " * 4
) + "</p></article>"


def response(body, url="https://example.org/notice", kind="text/html", status=200):
    result = Mock(status_code=status, url=url, headers={"Content-Type": kind}, encoding="utf-8")
    result.iter_content.return_value = [body]
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


@pytest.mark.parametrize("value,text,url", [
    ("", "", None),
    ("https://www.federalregister.gov/d/2021-01771", "", "https://www.federalregister.gov/d/2021-01771"),
    ('"Withdrawal: "https://www.federalregister.gov/d/2021-01771', "", "https://www.federalregister.gov/d/2021-01771"),
    ("Withdrawal: https://www.federalregister.gov/d/2020-11848.", "", "https://www.federalregister.gov/d/2020-11848"),
    ("This AD applies to Model R22 helicopters.", "This AD applies to Model R22 helicopters.", None),
])
def test_column_r_classification(value, text, url):
    assert split_source_field(value) == (text, url)


def test_manufacturer_link_in_regulatory_text_is_not_a_notice_citation():
    text = "This proposed AD applies to the following aircraft. " * 6 + "See https://www.myboeingfleet.com."
    assert split_source_field(text) == (text, None)


def test_mixed_text_and_link_does_not_include_literal_newlines_in_url():
    text = "https://www.federalregister.gov/d/2021-21050\\nWithdrawal:\\n" + "Proposed regulatory text. " * 10
    assert split_source_field(text) == (text.strip(), "https://www.federalregister.gov/d/2021-21050")


def test_long_fallback_paragraph_is_not_truncated():
    text = "Regulatory evidence " * 200 + "END OF SOURCE"
    assert _split_fulltext(text)[0][2] == text


def test_downloaded_html_is_cached_and_keeps_paragraph_ids(tmp_path):
    session = Mock(headers={}, get=Mock(return_value=response(NOTICE.encode())))
    fetcher = SourceFetcher(tmp_path, session=session)
    first = fetcher.fetch("https://example.org/notice")
    second = fetcher.fetch("https://example.org/notice")
    assert first == second
    assert session.get.call_count == 1
    assert first["blocks"][1]["source_element_id"] == "p-1"
    assert first["blocks"][1]["section"] == "Applicability"
    assert first["blocks"][1]["page"] is None
    assert first["blocks"][1]["page_kind"] == "html_paragraph"
    assert len(list(tmp_path.glob("*.body"))) == 1


def test_federal_register_api_resolves_full_text(tmp_path):
    source = "https://www.federalregister.gov/d/2021-01771"
    body_url = "https://www.federalregister.gov/documents/full_text/html/2021/01/28/2021-01771.html"
    api = {"body_html_url": body_url, "html_url": source, "document_number": "2021-01771",
           "pdf_url": "https://www.govinfo.gov/content/pkg/FR-2021-01-28/pdf/2021-01771.pdf"}
    session = Mock(headers={})
    session.get.side_effect = [response(json.dumps(api).encode(), kind="application/json"),
                               response(NOTICE.encode(), url=body_url)]
    result = SourceFetcher(tmp_path, session=session).fetch(source)
    assert result["source_url"] == source
    assert result["resolved_url"] == body_url
    assert result["official_pdf_url"] == api["pdf_url"]
    assert result["federal_register_document_number"] == "2021-01771"


def test_html_excludes_site_help_and_preserves_explicit_page_numbers():
    html = '<div class="preamble"><div class="fr-seal-block"><p>Document headings help text</p></div>'
    html += '<h1>SUMMARY:</h1><p id="p-1" data-page="10504">' + "Actual notice evidence. " * 15 + "</p></div>"
    blocks, _ = SourceFetcher._html_blocks(html, "https://www.federalregister.gov/documents/full_text/html/example.html")
    assert "help text" not in " ".join(block["text"] for block in blocks)
    assert blocks[-1]["page"] == 10504
    assert blocks[-1]["page_kind"] == "federal_register_page"


def test_landing_page_follows_document_pdf_and_preserves_pdf_page(tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 700 Td (Model R22 applicability evidence.) Tj ET")
    page[NameObject("/Contents")] = content
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    session = Mock(headers={})
    session.get.side_effect = [response(b'<html><body><a href="/notice.pdf">Download PDF</a></body></html>'),
                               response(buffer.getvalue(), url="https://example.org/notice.pdf", kind="application/pdf")]
    result = SourceFetcher(tmp_path, session=session).fetch("https://example.org/notice")
    assert result["format"] == "pdf"
    assert result["blocks"][0]["page"] == 1
    assert "R22" in result["blocks"][0]["text"]
    assert result["blocks"][-1]["page"] == 2
    assert result["blocks"][-1]["low_confidence"]


def test_blocked_or_submission_html_is_not_evidence(tmp_path):
    session = Mock(headers={}, get=Mock(return_value=response(b"<html><main><p>Verify you are human</p></main></html>")))
    with pytest.raises(SourceError, match="No reliable notice text"):
        SourceFetcher(tmp_path, session=session).fetch("https://example.org/notice")


def test_download_limit_and_http_failures_do_not_create_cache(tmp_path):
    session = Mock(headers={}, get=Mock(return_value=response(b"oversized")))
    with pytest.raises(SourceError, match="download limit"):
        SourceFetcher(tmp_path, session=session, max_bytes=2).download("https://example.org/notice")
    session.get.return_value = response(b"not found", status=404)
    with pytest.raises(SourceError, match="404"):
        SourceFetcher(tmp_path, session=session).download("https://example.org/notice")
    assert not list(tmp_path.iterdir())


def test_failed_download_retains_document_summary():
    doc = {"doc_id": "id", "summary": "Original summary", "url": "https://example.org/notice", "has_fulltext": False}
    fetcher = Mock(fetch=Mock(side_effect=SourceError("Source returned HTTP 404.")))
    record = prepare_document(doc, fetcher)
    assert record["summary"] == "Original summary"
    assert record["source"]["status"] == "unavailable"
    assert not record["has_fulltext"]
    assert "source" not in doc


def test_real_corpus_has_unique_ids_and_prepares_embedded_text_offline():
    docs, _ = load_tsv("data/ad_nprm.tsv")
    assert len(docs) == len({d["doc_id"] for d in docs}) == 431
    doc = next(d for d in docs if d["docket"] == "FAA-2012-1319" and d["embedded_text"])
    record = prepare_document(doc)
    assert record["source"]["status"] == "embedded"
    assert record["source"]["text"] == doc["embedded_text"].replace("\\n", "\n").replace("\\t", "\t")
    assert any(b["section"] == "Applicability" for b in record["source"]["blocks"])
    assert all(b["page"] is None for b in record["source"]["blocks"])


def test_replacement_workbook_exports_with_stable_ids_dates_and_revision_links(tmp_path):
    from aerotrace.import_dataset import ROOT, export_workbook

    manifest = export_workbook(ROOT / "data" / "challenge4_ad_index.xlsx", tmp_path)
    docs, _ = load_tsv(tmp_path / "ad_nprm.tsv")
    assert manifest["sheets"]["nprm_index"]["records"] == 431
    assert manifest["sheets"]["revision_chains"]["records"] == 207
    assert docs[0]["doc_id"] == docs[0]["record_id"] == "NPRM-001"
    assert docs[0]["docket"] == "FAA-2026-11814"
    assert docs[0]["publish_date"] == date(2026, 10, 19)
    assert docs[0]["source_column"] == "federal_register_link"
    assert docs[0]["comments_due"] is None  # The new index no longer supplies this field.
    revision = next(d for d in docs if d["record_id"] == "NPRM-011")
    assert revision["dataset_revision"]["status"] == "latest"
    assert revision["dataset_revision"]["versions_total"] == "2"
    assert sum(bool(d["supersedes_ad"]) for d in docs) == 62
