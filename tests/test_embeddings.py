from unittest.mock import Mock

import pytest
import requests

from aerotrace.embeddings import EmbeddingError, OpenRouterEmbeddings


def response(data, status=200, headers=None):
    return Mock(status_code=status, headers=headers or {}, json=Mock(return_value=data))


def test_batches_restore_input_order_and_sum_usage():
    session = Mock()
    session.post.side_effect = [
        response({"data": [{"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]}],
                  "usage": {"prompt_tokens": 8}}),
        response({"data": [{"index": 0, "embedding": [1., 1.]}], "usage": {"prompt_tokens": 4}}),
    ]
    client = OpenRouterEmbeddings(api_key="test-secret", dimensions=2, batch_size=2, session=session)
    assert client.embed(["first", "second", "third"]) == [[1., 0.], [0., 1.], [1., 1.]]
    assert client.last_usage == {"prompt_tokens": 12}
    assert session.post.call_count == 2
    assert session.post.call_args.kwargs["json"]["dimensions"] == 2
    assert session.post.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("texts", ["a string", [""], ["ok", "  "], [None]])
def test_invalid_input_never_calls_api(texts):
    session = Mock()
    client = OpenRouterEmbeddings(api_key="test-secret", session=session)
    with pytest.raises(ValueError):
        client.embed(texts)
    session.post.assert_not_called()


@pytest.mark.parametrize("data", [
    {"data": []},
    {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}]},
    {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [1]}]},
    {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [float("nan"), 1]}]},
    {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 0]}]},
])
def test_bad_responses_are_rejected(data):
    client = OpenRouterEmbeddings(api_key="test-secret", session=Mock(post=Mock(return_value=response(data))))
    with pytest.raises(EmbeddingError):
        client.embed(["one", "two"])


def test_rate_limit_retries_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("aerotrace.embeddings.time.sleep", sleeps.append)
    session = Mock()
    session.post.side_effect = [response({}, 429, {"Retry-After": "2"}),
                                response({"data": [{"index": 0, "embedding": [1, 0]}]})]
    assert OpenRouterEmbeddings(api_key="secret", session=session).embed(["one"]) == [[1, 0]]
    assert sleeps == [2]


def test_errors_do_not_expose_key_or_provider_body():
    session = Mock(post=Mock(return_value=response({"error": "test-secret"}, 401)))
    with pytest.raises(EmbeddingError) as error:
        OpenRouterEmbeddings(api_key="test-secret", session=session).embed(["one"])
    assert "401" in str(error.value)
    assert "test-secret" not in str(error.value)
    session.post.return_value.json.assert_not_called()


def test_network_failure_is_bounded_and_sanitized(monkeypatch):
    monkeypatch.setattr("aerotrace.embeddings.time.sleep", lambda _: None)
    session = Mock(post=Mock(side_effect=requests.Timeout("test-secret")))
    with pytest.raises(EmbeddingError, match="network error") as error:
        OpenRouterEmbeddings(api_key="test-secret", session=session, max_retries=1).embed(["one"])
    assert session.post.call_count == 2
    assert "test-secret" not in str(error.value)


def test_env_loading_preserves_existing_environment(tmp_path, monkeypatch):
    from aerotrace import config

    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "existing")
    config.load_env()
    assert OpenRouterEmbeddings().api_key == "existing"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    config.load_env()
    assert OpenRouterEmbeddings().api_key == "from-file"


def test_openrouter_formatted_key_alias_is_supported_without_using_direct_openai_keys(monkeypatch):
    monkeypatch.setattr("aerotrace.embeddings.load_env", lambda: None)
    monkeypatch.setattr("aerotrace.embeddings.project_openrouter_alias", lambda: "")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-test-value")
    assert OpenRouterEmbeddings().api_key == "sk-or-test-value"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-direct-openai-value")
    with pytest.raises(EmbeddingError, match="OPENROUTER_API_KEY"):
        OpenRouterEmbeddings()
    monkeypatch.setenv("OPENROUTER_API_KEY", "explicit-openrouter-value")
    assert OpenRouterEmbeddings().api_key == "explicit-openrouter-value"


def test_project_alias_is_not_masked_by_global_direct_openai_key(monkeypatch):
    monkeypatch.setattr("aerotrace.embeddings.load_env", lambda: None)
    monkeypatch.setattr("aerotrace.embeddings.project_openrouter_alias", lambda: "sk-or-project-value")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-global-direct-key")
    assert OpenRouterEmbeddings().api_key == "sk-or-project-value"
    assert __import__("os").environ["OPENAI_API_KEY"] == "sk-proj-global-direct-key"
