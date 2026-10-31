from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np

from .config import Settings
from .schema import connect


class OpenRouterEmbeddings:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.openrouter_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

    def embed(self, texts: list[str], *, input_type: str) -> np.ndarray:
        response = httpx.post(
            f"{self.settings.openrouter_url.rstrip('/')}/embeddings",
            headers={
                "Authorization": f"Bearer {self.settings.openrouter_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://aerotrace.local",
                "X-Title": "AeroTrace",
            },
            json={
                "model": self.settings.embed_model_slug,
                "input": texts,
                "encoding_format": "float",
                "input_type": input_type,
            },
            timeout=90,
        )
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda item: item["index"])
        vectors = np.asarray([item["embedding"] for item in data], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-12)


def build_embeddings(settings: Settings, *, batch_size: int = 64) -> dict:
    con = connect(settings.database)
    rows = con.execute("SELECT chunk_id,text_hash,searchable_text FROM chunks ORDER BY chunk_id").fetchall()
    old_manifest = {}
    old_vectors = None
    if settings.vector_manifest.exists() and settings.vectors.exists():
        payload = json.loads(settings.vector_manifest.read_text(encoding="utf-8"))
        if payload.get("model") == settings.embed_model_slug:
            old_manifest = {item["chunk_id"]: (i, item) for i, item in enumerate(payload.get("chunks", []))}
            old_vectors = np.load(settings.vectors)

    vectors: list[np.ndarray | None] = [None] * len(rows)
    pending: list[tuple[int, str]] = []
    reused = 0
    for i, row in enumerate(rows):
        old = old_manifest.get(row["chunk_id"])
        if old and old[1].get("text_hash") == row["text_hash"] and old_vectors is not None:
            vectors[i] = old_vectors[old[0]]
            reused += 1
        else:
            pending.append((i, row["searchable_text"]))

    client = OpenRouterEmbeddings(settings)
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        embedded = client.embed([text for _, text in batch], input_type="search_document")
        for (position, _), vector in zip(batch, embedded):
            vectors[position] = vector
    if not rows:
        matrix = np.empty((0, 0), dtype=np.float32)
    else:
        matrix = np.stack(vectors).astype(np.float32)  # type: ignore[arg-type]
    settings.artifacts.mkdir(parents=True, exist_ok=True)
    np.save(settings.vectors, matrix)
    manifest = {
        "model": settings.embed_model_slug,
        "dimensions": int(matrix.shape[1]) if matrix.ndim == 2 and matrix.size else 0,
        "chunks": [{"chunk_id": row["chunk_id"], "text_hash": row["text_hash"]} for row in rows],
    }
    settings.vector_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    con.close()
    return {"chunks": len(rows), "embedded": len(pending), "reused": reused, "dimensions": manifest["dimensions"]}


class VectorIndex:
    def __init__(self, settings: Settings):
        if not settings.vector_manifest.exists() or not settings.vectors.exists():
            raise RuntimeError("Vector index is missing. Run scripts/build_index.py first.")
        manifest = json.loads(settings.vector_manifest.read_text(encoding="utf-8"))
        if manifest["model"] != settings.embed_model_slug:
            raise RuntimeError("Embedding model changed; rebuild the vector index.")
        self.chunk_ids = [item["chunk_id"] for item in manifest["chunks"]]
        self.vectors = np.load(settings.vectors, mmap_mode="r")
        self.client = OpenRouterEmbeddings(settings)

    def search(self, query: str, allowed: set[str] | None = None, k: int = 100) -> list[tuple[str, float]]:
        q = self.client.embed([query], input_type="search_query")[0]
        similarities = np.asarray(self.vectors @ q)
        order = np.argsort(-similarities)
        out = []
        for index in order:
            chunk_id = self.chunk_ids[int(index)]
            if allowed is not None and chunk_id not in allowed:
                continue
            out.append((chunk_id, float(similarities[index])))
            if len(out) >= k:
                break
        return out

