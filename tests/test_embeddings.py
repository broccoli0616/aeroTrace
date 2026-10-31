"""OpenAI embedding backend, tested offline with a fake client (no key or network needed)."""
import hashlib
import types

import numpy as np
import pytest

from aerotrace.index import HybridIndex

PASSAGES = [{"doc_id": "A", "text": "Inspect the tail rotor blade for cracks."},
            {"doc_id": "B", "text": "Replace the fuel pump seal on the left engine."}]
DOCS = {"A": {"title": "Rotor AD", "make": "Robinson", "models": ["R22"], "docket": "FAA-1"},
        "B": {"title": "Fuel AD", "make": "Airbus", "models": ["A330"], "docket": "FAA-2"}}


def fake_vec(text):
    # deterministic pseudo-embedding: words hashed into 64 buckets
    v = np.zeros(64)
    for w in text.lower().split():
        v[int(hashlib.md5(w.strip(".?").encode()).hexdigest(), 16) % 64] += 1
    return v.tolist()


class FakeOpenAI:
    calls = 0
    base_url = model = None

    def __init__(self, api_key=None, base_url=None):
        self.embeddings = self
        FakeOpenAI.base_url = base_url

    def create(self, model, input):
        FakeOpenAI.calls += 1
        FakeOpenAI.model = model
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=fake_vec(t)) for t in input])


@pytest.fixture
def openai_env(monkeypatch, tmp_path):
    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("AEROTRACE_EMBEDDINGS", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.chdir(tmp_path)  # cache goes to tmp_path/.cache
    FakeOpenAI.calls = 0


def test_openai_semantic_scores(openai_env):
    idx = HybridIndex(PASSAGES, DOCS)
    assert idx.embed_backend == "openai/text-embedding-3-small"
    hits = idx.search("crack in tail rotor blade")
    assert hits[0][0] == 0
    sims = {i: sim for i, _, sim in hits}
    assert sims[0] > sims.get(1, -1.0)


def test_openai_vectors_cached(openai_env):
    HybridIndex(PASSAGES, DOCS)
    first = FakeOpenAI.calls
    HybridIndex(PASSAGES, DOCS)
    assert FakeOpenAI.calls == first  # second build loads .cache instead of calling the API


def test_missing_key_falls_back_to_bm25(openai_env, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    idx = HybridIndex(PASSAGES, DOCS)
    assert idx.vectors is None and idx.embed_backend is None
    assert idx.search("fuel pump")[0][2] is None


def test_openrouter_key_routes_to_openrouter(openai_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-v1-test")
    idx = HybridIndex(PASSAGES, DOCS)
    assert FakeOpenAI.base_url == "https://openrouter.ai/api/v1"
    assert FakeOpenAI.model == "openai/text-embedding-3-small"
    assert idx.embed_backend == "openrouter/openai/text-embedding-3-small"
