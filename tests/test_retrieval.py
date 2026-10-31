import json

import numpy as np
import pytest

from aerotrace.retrieval import RetrievalError, TwoStageIndex, build_index, make_corpus, split_text


class FakeEmbeddings:
    model = "test-embedding"
    dimensions = 3
    batch_size = 4

    def __init__(self):
        self.calls = []
        self.last_usage = {}

    def embed(self, texts):
        self.calls.append(list(texts))
        self.last_usage = {"prompt_tokens": len(texts)}
        vectors = []
        for text in texts:
            if text == "alpha question" or "winning passage" in text or "outside candidate passage" in text:
                vectors.append([1., 0., 0.])
            elif "summary alpha" in text:
                vectors.append([0.9, 0.1, 0.])
            elif "summary beta" in text:
                vectors.append([0.1, 0.9, 0.])
            elif "summary gamma" in text:
                vectors.append([-1., 0., 0.])
            else:
                vectors.append([0., 0., 1.])
        return vectors


def records():
    return [{"doc_id": name, "record_id": name, "title": f"Document {name}", "docket": f"FAA-{name}",
             "summary": f"summary {name}", "make": "Manufacturer", "models": [], "revision": "Original NPRM",
             "publish_date": "2026-01-01", "fr_citation": "91 FR 123", "pages": "123-125", "url": None,
             "source": {"status": "embedded", "blocks": [{
                 "paragraph": "(c)", "section": "Applicability", "page": 2, "page_kind": "pdf_page",
                 "text": passage, "low_confidence": False,
             }]}}
            for name, passage in [("alpha", "winning passage"), ("beta", "other passage"),
                                  ("gamma", "outside candidate passage")]]


def prepared(tmp_path):
    path = tmp_path / "documents.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records()), encoding="utf-8")
    return path


def test_chunking_preserves_every_unicode_character_without_overlap():
    text = ("Aircraft inspection. 飞机维修检查。✈️ Maintenance torque 12–15 Nm. " * 30) + "END"
    chunks = split_text(text, max_tokens=32, overlap=0)
    assert "".join(chunk["text"] for chunk in chunks) == text
    assert len(chunks) > 1
    assert all(chunk["token_end"] - chunk["token_start"] <= 32 for chunk in chunks)
    assert all("\ufffd" not in chunk["text"] for chunk in chunks)


def test_chunk_overlap_preserves_source_location_and_never_crosses_pages():
    rows = records()[:1]
    rows[0]["source"]["blocks"] = [
        {"paragraph": f"p{page}", "section": "Inspection", "page": page, "page_kind": "pdf_page",
         "text": f"PAGE{page} inspection " * 40} for page in [1, 2]
    ]
    _, chunks = make_corpus(rows, chunk_tokens=32, overlap=8)
    document_chunks = [c for c in chunks if c["evidence_kind"] == "document"]
    assert {c["page"] for c in document_chunks} == {1, 2}
    for chunk in document_chunks:
        assert chunk["paragraph"] == f"p{chunk['page']}"
        assert f"PAGE{3 - chunk['page']}" not in chunk["text"]
    page1 = [c for c in document_chunks if c["page"] == 1]
    assert page1[1]["token_start"] < page1[0]["token_end"]


def test_missing_summary_and_source_are_explicitly_labelled():
    rows = records()[:1]
    rows[0]["summary"] = ""
    rows[0]["source"] = {"status": "not_fetched", "blocks": []}
    docs, chunks = make_corpus(rows)
    assert docs[0]["summary_basis"] == "metadata"
    assert docs[0]["source_status"] == "not_fetched"
    assert {c["evidence_kind"] for c in chunks} == {"metadata"}
    assert all(c["page"] is None for c in chunks)


def test_stage_two_is_restricted_to_top_n_and_preserves_citations(tmp_path):
    client = FakeEmbeddings()
    build_index(prepared(tmp_path), tmp_path / "index", client=client)
    client.calls.clear()
    index = TwoStageIndex(tmp_path / "index", client=client)
    result = index.search("alpha question", top_n=1, top_m=10)
    assert [d["doc_id"] for d in result["documents"]] == ["alpha"]
    assert {c["doc_id"] for c in result["chunks"]} == {"alpha"}
    # Gamma's passage would score highest globally, but its summary excluded that document.
    assert result["chunks"][0]["text"] == "winning passage"
    assert result["chunks"][0]["page"] == 2
    assert result["chunks"][0]["paragraph"] == "(c)"
    assert client.calls == [["alpha question"]]
    assert not result["query_embedding_cached"]
    again = index.search("alpha question", top_n=2, top_m=2)
    assert again["query_embedding_cached"]
    assert client.calls == [["alpha question"]]
    assert len(again["documents"]) == len(again["chunks"]) == 2
    assert {c["doc_id"] for c in again["chunks"]} <= {d["doc_id"] for d in again["documents"]}


def test_rebuild_reuses_vectors_and_persists_normalised_matrices(tmp_path):
    client = FakeEmbeddings()
    path, output = prepared(tmp_path), tmp_path / "index"
    first = build_index(path, output, client=client)
    assert first["embedding_cache"]["new_inputs"] > 0
    # The summary's chunk and document use identical text, so they share one cached vector.
    assert first["embedding_cache"]["unique_inputs"] < first["embedding_cache"]["total_inputs"]
    client.calls.clear()
    second = build_index(path, output, client=client)
    assert second["embedding_cache"]["new_inputs"] == 0
    assert client.calls == []
    index = TwoStageIndex(output, client=client)
    assert np.allclose(np.linalg.norm(index.summary_vectors, axis=1), 1)
    assert np.allclose(np.linalg.norm(index.chunk_vectors, axis=1), 1)


def test_changed_prepared_corpus_requires_rebuild(tmp_path):
    path, output = prepared(tmp_path), tmp_path / "index"
    build_index(path, output, client=FakeEmbeddings())
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(RetrievalError, match="changed"):
        TwoStageIndex(output, client=FakeEmbeddings())


def test_outdated_prepared_record_is_rejected_before_build_api(tmp_path):
    from aerotrace.config import ROOT
    from aerotrace.ingest import load_tsv
    from aerotrace.prepare import prepare_document

    record = json.loads(json.dumps(prepare_document(load_tsv(ROOT / "data" / "ad_nprm.tsv")[0][0]), default=str))
    record["summary"] = "An outdated summary from an earlier dataset."
    record["source_record"] = {"path": str(ROOT / "data" / "ad_nprm.tsv")}
    path = tmp_path / "outdated.jsonl"
    path.write_text(json.dumps(record), encoding="utf-8")
    client = FakeEmbeddings()
    with pytest.raises(RetrievalError, match="do not match"):
        build_index(path, tmp_path / "index", client=client)
    assert not client.calls


def test_model_or_dimension_mismatch_rejected_before_query_api(tmp_path):
    path, output = prepared(tmp_path), tmp_path / "index"
    build_index(path, output, client=FakeEmbeddings())
    client = FakeEmbeddings()
    client.model = "different-model"
    index = TwoStageIndex(output, client=client)
    with pytest.raises(RetrievalError, match="model and dimensions"):
        index.search("alpha question")
    assert not client.calls


@pytest.mark.parametrize("query,n,m", [("", 1, 1), ("alpha", 0, 1), ("alpha", 1, 0), ("alpha", True, 1)])
def test_invalid_search_never_calls_api(tmp_path, query, n, m):
    client = FakeEmbeddings()
    build_index(prepared(tmp_path), tmp_path / "index", client=client)
    client.calls.clear()
    with pytest.raises(ValueError):
        TwoStageIndex(tmp_path / "index", client=client).search(query, n, m)
    assert not client.calls


def test_document_filter_and_large_requested_limits(tmp_path):
    client = FakeEmbeddings()
    build_index(prepared(tmp_path), tmp_path / "index", client=client)
    index = TwoStageIndex(tmp_path / "index", client=client)
    result = index.search("alpha question", 100, 100, allowed_doc_ids={"beta"})
    assert [d["doc_id"] for d in result["documents"]] == ["beta"]
    assert {c["doc_id"] for c in result["chunks"]} == {"beta"}
    with pytest.raises(ValueError, match="No indexed documents"):
        index.search("alpha question", allowed_doc_ids=set())


def test_explicit_docket_or_record_is_a_constraint_not_a_fuzzy_number_match(tmp_path):
    rows = records()
    rows[0]["docket"] = "FAA-2026-11814"
    rows[0]["doc_id"] = rows[0]["record_id"] = "NPRM-001"
    rows[1]["docket"] = "FAA-2026-11795"
    path = tmp_path / "documents.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    client = FakeEmbeddings()
    build_index(path, tmp_path / "index", client=client)
    index = TwoStageIndex(tmp_path / "index", client=client)
    for question in ["What does FAA-2026-11814 propose?", "Show NPRM-001"]:
        result = index.search(question, 20, 6)
        assert [d["doc_id"] for d in result["documents"]] == ["NPRM-001"]
        assert {c["doc_id"] for c in result["chunks"]} == {"NPRM-001"}
        assert result["reference_filter"]
    client.calls.clear()
    with pytest.raises(ValueError, match="explicit reference"):
        index.search("What does FAA-2099-12345 propose?")
    assert not client.calls


def test_failed_build_does_not_publish_partial_index(tmp_path):
    path, output = prepared(tmp_path), tmp_path / "index"
    build_index(path, output, client=FakeEmbeddings())
    before = (output / "manifest.json").read_bytes()
    client = FakeEmbeddings()
    client.model = "new-model"

    def fail(_):
        raise RuntimeError("API unavailable")

    client.embed = fail
    with pytest.raises(RuntimeError, match="API unavailable"):
        build_index(path, output, client=client)
    assert (output / "manifest.json").read_bytes() == before
