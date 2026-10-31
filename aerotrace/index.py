"""Hybrid retrieval: BM25 keyword search, plus optional semantic vectors merged with reciprocal rank fusion.

BM25 is pure Python and needs no downloads. A vector index for semantic matches is added when
AEROTRACE_EMBEDDINGS is set:
- AEROTRACE_EMBEDDINGS=openai: OpenAI embeddings (needs OPENAI_API_KEY; model from AEROTRACE_EMBED_MODEL,
  default text-embedding-3-small). Passage vectors are cached in .cache/ so restarts don't re-embed.
- AEROTRACE_EMBEDDINGS=1: local sentence-transformers (all-MiniLM-L6-v2).
If the backend is unavailable, retrieval silently falls back to BM25 only.
"""
import hashlib
import math
import os
from collections import Counter, defaultdict
from pathlib import Path

from .text import tokenize


class BM25:
    def __init__(self, texts: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_tf = [Counter(tokenize(t)) for t in texts]
        self.doc_len = [sum(tf.values()) for tf in self.doc_tf]
        self.avgdl = sum(self.doc_len) / max(1, len(self.doc_len))
        df = Counter(term for tf in self.doc_tf for term in tf)
        n = len(texts)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.postings = defaultdict(list)
        for i, tf in enumerate(self.doc_tf):
            for term in tf:
                self.postings[term].append(i)

    def search(self, query: str, allowed: set[int] | None = None, k: int = 50) -> list[tuple[int, float]]:
        scores = defaultdict(float)
        for term in set(tokenize(query)):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i in self.postings[term]:
                if allowed is not None and i not in allowed:
                    continue
                tf = self.doc_tf[i][term]
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                scores[i] += idf * tf * (self.k1 + 1) / denom
        return sorted(scores.items(), key=lambda kv: -kv[1])[:k]


class HybridIndex:
    def __init__(self, passages: list[dict], docs_by_id: dict):
        self.passages = passages
        # Index passage text together with its document title, make and models so "Boeing 787" style queries hit.
        texts = [
            f"{p['text']} {docs_by_id[p['doc_id']]['title']} {docs_by_id[p['doc_id']]['make']} "
            f"{' '.join(docs_by_id[p['doc_id']]['models'])} {docs_by_id[p['doc_id']]['docket']}"
            for p in passages
        ]
        self.bm25 = BM25(texts)
        self.vectors = None
        self.embed_backend = None
        backend = os.getenv("AEROTRACE_EMBEDDINGS", "").lower()
        try:
            if backend == "openai":
                self._init_openai(texts)
            elif backend == "1":
                from sentence_transformers import SentenceTransformer

                encoder = SentenceTransformer("all-MiniLM-L6-v2")
                self._embed = lambda batch: encoder.encode(batch, normalize_embeddings=True, batch_size=64)
                self.vectors = self._embed(texts)
                self.embed_backend = "sentence-transformers/all-MiniLM-L6-v2"
        except Exception as e:  # missing package, bad key, network error: keep working on BM25
            print(f"embeddings disabled ({type(e).__name__}: {e}); using BM25 only")
            self.vectors = None

    def _init_openai(self, texts: list[str]):
        import numpy as np
        from openai import OpenAI

        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        # OpenRouter keys (sk-or-...) use OpenRouter's OpenAI-compatible endpoint and vendor-prefixed model names.
        openrouter = key.startswith("sk-or-")
        base_url = os.getenv("OPENAI_BASE_URL") or ("https://openrouter.ai/api/v1" if openrouter else None)
        client = OpenAI(api_key=key, base_url=base_url)
        model = os.getenv("AEROTRACE_EMBED_MODEL") or (
            "openai/text-embedding-3-small" if openrouter else "text-embedding-3-small")

        def embed(batch: list[str]):
            vecs = []
            for start in range(0, len(batch), 200):  # well under the 2048-input / 300k-token request limits
                resp = client.embeddings.create(model=model, input=batch[start:start + 200])
                vecs.extend(d.embedding for d in resp.data)
            v = np.asarray(vecs, dtype=np.float32)
            return v / np.linalg.norm(v, axis=1, keepdims=True)

        self._embed = embed
        # Cache keyed on model + exact corpus text, so a changed corpus is re-embedded automatically.
        digest = hashlib.sha256((model + "\x00" + "\x00".join(texts)).encode()).hexdigest()[:16]
        cache = Path(".cache") / f"openai_{model.replace('/', '_')}_{digest}.npy"
        if cache.exists():
            self.vectors = np.load(cache)
        else:
            self.vectors = embed(texts)
            cache.parent.mkdir(exist_ok=True)
            np.save(cache, self.vectors)
        self.embed_backend = f"{'openrouter' if openrouter else 'openai'}/{model}"

    def search(self, query: str, allowed: set[int] | None = None, k: int = 30) -> list[tuple[int, float, float | None]]:
        """Returns (passage_index, bm25_score, semantic_similarity). With vectors, order comes from RRF but BM25
        scores are kept so the engine's evidence threshold still means 'keywords actually matched'.
        semantic_similarity is the cosine similarity between query and passage, or None without vectors."""
        kw = self.bm25.search(query, allowed, k=100)
        if self.vectors is None:
            return [(i, s, None) for i, s in kw[:k]]
        import numpy as np

        try:
            q = self._embed([query])[0]
        except Exception as e:  # API hiccup on one query shouldn't break search
            print(f"query embedding failed ({type(e).__name__}: {e}); using BM25 only for this query")
            return [(i, s, None) for i, s in kw[:k]]
        sims = self.vectors @ q
        order = [i for i in np.argsort(-sims) if allowed is None or i in allowed][:100]
        rrf = defaultdict(float)
        for rank, (i, _) in enumerate(kw):
            rrf[i] += 1 / (60 + rank)
        for rank, i in enumerate(order):
            rrf[int(i)] += 1 / (60 + rank)
        kw_score = dict(kw)
        return [(i, kw_score.get(i, 0.0), float(sims[i]))
                for i, _ in sorted(rrf.items(), key=lambda kv: -kv[1])[:k]]
