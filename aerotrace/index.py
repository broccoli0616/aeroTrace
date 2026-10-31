"""Hybrid retrieval: BM25 keyword search, plus optional semantic vectors merged with reciprocal rank fusion.

BM25 is pure Python and needs no downloads. If `sentence-transformers` is installed and
AEROTRACE_EMBEDDINGS=1 is set, a vector index is added for semantic matches.
"""
import math
import os
import sys
from collections import Counter, defaultdict

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
    def __init__(self, passages: list[dict], docs_by_id: dict, enable_vectors: bool | None = None):
        self.passages = passages
        # Index passage text together with its document title, make and models so "Boeing 787" style queries hit.
        texts = [
            f"{p['text']} {docs_by_id[p['doc_id']]['title']} {docs_by_id[p['doc_id']]['make']} "
            f"{' '.join(docs_by_id[p['doc_id']]['models'])} {docs_by_id[p['doc_id']]['docket']} {p['doc_id']}"
            for p in passages
        ]
        self.bm25 = BM25(texts)
        self.vectors = None
        use_vectors = os.getenv("AEROTRACE_EMBEDDINGS") == "1" if enable_vectors is None else enable_vectors
        if use_vectors:
            try:
                from sentence_transformers import SentenceTransformer

                self.encoder = SentenceTransformer("all-MiniLM-L6-v2")
                self.vectors = self.encoder.encode(texts, normalize_embeddings=True, batch_size=64)
            except ImportError:
                print("sentence-transformers not installed; using BM25 only", file=sys.stderr)

    def search(self, query: str, allowed: set[int] | None = None, k: int = 30) -> list[tuple[int, float]]:
        """Returns (passage_index, bm25_score). With vectors, order comes from RRF but BM25 scores are kept
        so the engine's evidence threshold still means 'keywords actually matched'."""
        kw = self.bm25.search(query, allowed, k=100)
        if self.vectors is None:
            return kw[:k]
        import numpy as np

        q = self.encoder.encode([query], normalize_embeddings=True)[0]
        sims = self.vectors @ q
        order = [i for i in np.argsort(-sims) if allowed is None or i in allowed][:100]
        rrf = defaultdict(float)
        for rank, (i, _) in enumerate(kw):
            rrf[i] += 1 / (60 + rank)
        for rank, i in enumerate(order):
            rrf[int(i)] += 1 / (60 + rank)
        kw_score = dict(kw)
        return [(i, kw_score.get(i, 0.0)) for i, _ in sorted(rrf.items(), key=lambda kv: -kv[1])[:k]]
