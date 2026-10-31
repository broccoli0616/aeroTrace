import csv
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import anthropic
import pytest

from aerotrace import compose
from aerotrace.config import ClaudeConfig, ROOT
from aerotrace.engine import AeroTrace
from aerotrace.ingest import load_tsv
from aerotrace.text import sentences


@pytest.fixture
def claude(monkeypatch):
    client = MagicMock()
    client.__enter__.return_value = client
    factory = Mock(return_value=client)
    monkeypatch.setattr(anthropic, "Anthropic", factory)
    monkeypatch.setattr(compose, "claude_config", lambda: ClaudeConfig("test-secret", "test-claude"))
    return client, factory


def response(data, stop="end_turn"):
    text = data if isinstance(data, str) else json.dumps(data)
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


def test_claude_receives_complete_sources_and_uses_stable_json_api(claude):
    client, factory = claude
    text = "Long evidence " * 300 + "END OF SOURCE"
    client.messages.create.return_value = response({"answerable": True, "statements": [{"text": "END OF SOURCE", "passage_id": "p1"}]})
    statements, error = compose.llm("What is recorded?", [{"passage_id": "p1", "text": text}], context={"applicability": "UNKNOWN"})
    assert error is None and statements[0]["passage_id"] == "p1"
    payload = client.messages.create.call_args.kwargs
    assert payload["model"] == "test-claude"
    assert payload["output_config"]["format"]["type"] == "json_schema"
    assert "END OF SOURCE" in payload["messages"][0]["content"]
    assert "UNKNOWN" in payload["messages"][0]["content"]
    assert "betas" not in payload and "fallbacks" not in payload
    assert factory.call_args.kwargs["base_url"] == "https://api.anthropic.com"


@pytest.mark.parametrize("data", [
    "not JSON",
    {"answerable": "yes", "statements": []},
    {"answerable": True, "statements": [{"text": "A claim", "passage_id": "unknown"}]},
    {"answerable": True, "statements": [{"text": "", "passage_id": "p1"}]},
    {"answerable": True, "statements": [{"text": "A claim", "passage_id": "p1"}] * 5},
])
def test_malformed_or_uncited_output_is_rejected(claude, data):
    client, _ = claude
    client.messages.create.return_value = response(data)
    statements, error = compose.llm("Question", [{"passage_id": "p1", "text": "A claim"}])
    assert not statements and error


@pytest.mark.parametrize("stop", ["refusal", "max_tokens", "tool_use"])
def test_incomplete_or_refused_response_is_rejected(claude, stop):
    client, _ = claude
    client.messages.create.return_value = response({"answerable": False, "statements": []}, stop)
    assert compose.llm("Question", [{"passage_id": "p1", "text": "Source"}])[1]


def test_claude_errors_are_sanitized(claude):
    client, _ = claude
    http_response = Mock(status_code=401, headers={}, request=Mock())
    client.messages.create.side_effect = anthropic.AuthenticationError("test-secret", response=http_response, body={"secret": "test-secret"})
    statements, error = compose.llm("Question", [{"passage_id": "p1", "text": "Source"}])
    assert not statements and "401" in error and "test-secret" not in error


def test_missing_key_does_not_create_client(claude, monkeypatch):
    _, factory = claude
    monkeypatch.setattr(compose, "claude_config", lambda: ClaudeConfig(""))
    assert "ANTHROPIC_API_KEY" in compose.llm("Question", [{"passage_id": "p1", "text": "Source"}])[1]
    factory.assert_not_called()


def test_updated_project_env_is_read_without_exposing_or_overwriting_key(tmp_path, monkeypatch):
    from aerotrace import config

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-shell-value")
    path = tmp_path / ".env"
    path.write_text("ANTHROPIC_API_KEY=project-value\nAEROTRACE_MODEL=project-model\n", encoding="utf-8")
    first = config.claude_config()
    assert first.api_key == "project-value" and first.model == "project-model"
    assert "project-value" not in repr(first)
    path.write_text("ANTHROPIC_API_KEY=updated-value\nAEROTRACE_MODEL=new-model\n", encoding="utf-8")
    assert config.claude_config().api_key == "updated-value"
    assert config.claude_config().model == "new-model"
    assert __import__("os").environ["ANTHROPIC_API_KEY"] == "stale-shell-value"


@pytest.fixture
def semantic_engine(tmp_path):
    with (ROOT / "data" / "ad_nprm.tsv").open(encoding="utf-8") as stream:
        source = csv.DictReader(stream, delimiter="\t")
        fields, row = source.fieldnames, next(source)
    path = tmp_path / "data.tsv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerow(row)
    doc = load_tsv(path)[0][0]
    chunk = {"chunk_id": "NPRM-001:summary:0", "doc_id": "NPRM-001", "paragraph": "summary", "section": "Summary",
             "page": None, "text": doc["summary"], "evidence_kind": "summary", "source_url": doc["url"], "score": 0.8}
    index = Mock(chunks=[chunk])
    index.search.return_value = {"query": "Question", "chunks": [chunk], "documents": [{"doc_id": "NPRM-001"}]}
    return AeroTrace(path, tmp_path / "pdfs", semantic_index=index), doc


def test_semantic_chunks_feed_claude_and_return_checked_citations(semantic_engine, monkeypatch):
    engine, doc = semantic_engine
    quote = sentences(doc["summary"])[0]
    compose_mock = Mock(return_value=([{"text": quote, "passage_id": "NPRM-001:summary:0"}], None))
    monkeypatch.setattr(compose, "llm", compose_mock)
    result = engine.ask("What does FAA-2026-11814 propose?", mode="llm", top_n=7, top_m=3)
    assert result["status"] == "ANSWERED" and result["mode"] == "llm"
    assert result["retrieval_mode"] == "semantic" and result["llm_called"]
    assert result["statements"][0]["citations"][0]["passage_id"] == "NPRM-001:summary:0"
    assert result["statements"][0]["verification"]["verified"]
    assert [p["passage_id"] for p in compose_mock.call_args.args[1]] == ["NPRM-001:summary:0"]
    assert "revision_status" in compose_mock.call_args.kwargs["context"]
    engine.semantic_index.search.assert_called_once_with("What does FAA-2026-11814 propose?", 7, 3, allowed_doc_ids={"NPRM-001"})


def test_missing_configuration_stops_before_claude(semantic_engine, monkeypatch):
    engine, _ = semantic_engine
    composer = Mock()
    monkeypatch.setattr(compose, "llm", composer)
    result = engine.ask("Does FAA-2026-11814 apply to my helicopter?", mode="llm")
    assert result["reason_code"] == "MISSING_CONFIG" and not result["llm_called"]
    composer.assert_not_called()


def test_generated_unsupported_number_is_rejected(semantic_engine, monkeypatch):
    engine, _ = semantic_engine
    monkeypatch.setattr(compose, "llm", Mock(return_value=([
        {"text": "The inspection must occur every 9999 hours.", "passage_id": "NPRM-001:summary:0"}
    ], None)))
    result = engine.ask("What does FAA-2026-11814 propose?", mode="llm")
    assert result["reason_code"] == "VERIFICATION_FAILED" and not result["statements"]


def test_claude_overview_answer_is_not_rejected_for_missing_cover_or_docket(semantic_engine, monkeypatch):
    engine, doc = semantic_engine
    quote = sentences(doc["summary"])[0]
    monkeypatch.setattr(compose, "llm", Mock(return_value=([
        {"text": quote, "passage_id": "NPRM-001:summary:0"}
    ], None)))
    result = engine.ask("What does docket FAA-2026-11814 cover?", mode="llm")
    assert result["status"] == "ANSWERED" and result["mode"] == "llm"
    assert result["coverage_check"] == {"score": 1.0, "minimum": 0.5, "missing_terms": []}
    assert result["statements"][0]["verification"]["verified"]


def test_claude_failure_is_reported_as_extractive_fallback(semantic_engine, monkeypatch):
    engine, _ = semantic_engine
    monkeypatch.setattr(compose, "llm", Mock(return_value=([], "Claude connection failed.")))
    result = engine.ask("What does FAA-2026-11814 propose?", mode="llm")
    assert result["status"] == "ANSWERED" and result["mode"] == "extractive"
    assert any("LLM unavailable" in warning for warning in result["warnings"])
