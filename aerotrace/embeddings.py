"""OpenRouter embedding client and small, explicit API smoke test.

Run: python -m aerotrace.embeddings --text "Does this apply to the R22?"
The persistent two-stage index using this client is in aerotrace.retrieval.
"""
import argparse
import json
import math
import os
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from .config import load_env, project_openrouter_alias

ENDPOINT = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_MODEL = "openai/text-embedding-3-large"


class EmbeddingError(RuntimeError):
    """A safe-to-display API or response validation error."""


class OpenRouterEmbeddings:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 dimensions: int | None = None, batch_size: int = 32,
                 timeout: float = 60, max_retries: int = 2,
                 session: requests.Session | None = None):
        load_env()
        configured_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        compatible_key = os.getenv("OPENAI_API_KEY", "").strip()
        # Some OpenAI-compatible clients name an OpenRouter key OPENAI_API_KEY.
        # Only accept that alias for recognisable OpenRouter keys, never a direct OpenAI key.
        if not configured_key and compatible_key.startswith("sk-or-"):
            configured_key = compatible_key
        if api_key is None and not configured_key:
            configured_key = project_openrouter_alias()
        self.api_key = (api_key if api_key is not None else configured_key).strip()
        if not self.api_key:
            raise EmbeddingError("Set OPENROUTER_API_KEY in .env or your environment.")
        self.model = model or os.getenv("AEROTRACE_EMBEDDING_MODEL") or DEFAULT_MODEL
        if dimensions is None and os.getenv("AEROTRACE_EMBEDDING_DIMENSIONS"):
            try:
                dimensions = int(os.environ["AEROTRACE_EMBEDDING_DIMENSIONS"])
            except ValueError:
                raise ValueError("AEROTRACE_EMBEDDING_DIMENSIONS must be a positive integer.") from None
        if dimensions is not None and (type(dimensions) is not int or dimensions < 1):
            raise ValueError("dimensions must be positive")
        if batch_size < 1 or timeout <= 0 or max_retries < 0:
            raise ValueError("batch_size/timeout must be positive; max_retries must be nonnegative")
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.last_usage: dict = {}

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        value = response.headers.get("Retry-After", "") if response is not None else ""
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 2 ** attempt
        return min(30, max(0, delay))

    def _request(self, inputs: list[str]) -> dict:
        payload = {"model": self.model, "input": inputs, "encoding_format": "float"}
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    ENDPOINT, json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    timeout=(10, self.timeout), allow_redirects=False,
                )
            except requests.RequestException:
                if attempt == self.max_retries:
                    raise EmbeddingError("Embedding request failed after retries (network error or timeout).") from None
                time.sleep(self._retry_delay(None, attempt))
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                time.sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code != 200:
                hint = {401: "Check OPENROUTER_API_KEY.", 402: "Check your OpenRouter credit balance.",
                        413: "Reduce the input size.", 429: "Rate limit reached; retry later."}.get(
                            response.status_code, "Check the model, input size, and provider availability.")
                # Do not surface response bodies or request headers: either may contain sensitive values.
                raise EmbeddingError(f"OpenRouter returned HTTP {response.status_code}. {hint}")
            try:
                data = response.json()
            except ValueError:
                raise EmbeddingError("OpenRouter returned invalid JSON.") from None
            if not isinstance(data, dict) or "error" in data:
                raise EmbeddingError("OpenRouter returned an error or malformed response.")
            return data
        raise EmbeddingError("Embedding request failed.")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return finite, consistently sized vectors in input order; batch API calls.

        Blank inputs are rejected before any request. No inputs are silently truncated.
        last_usage contains summed numeric usage fields for this call.
        """
        if isinstance(texts, (str, bytes)):
            raise ValueError("Pass a list of strings, not a single string.")
        inputs = list(texts)
        if any(not isinstance(t, str) or not t.strip() for t in inputs):
            raise ValueError("Every embedding input must be a nonempty string.")
        self.last_usage = {}
        vectors = []
        expected_dimensions = self.dimensions
        for start in range(0, len(inputs), self.batch_size):
            batch = inputs[start:start + self.batch_size]
            response = self._request(batch)
            items = response.get("data")
            if not isinstance(items, list) or len(items) != len(batch):
                raise EmbeddingError("Embedding response has the wrong number of results.")
            ordered = [None] * len(batch)
            for item in items:
                if not isinstance(item, dict):
                    raise EmbeddingError("Embedding response contains an invalid result.")
                index, vector = item.get("index"), item.get("embedding")
                if type(index) is not int or not 0 <= index < len(batch) or ordered[index] is not None:
                    raise EmbeddingError("Embedding response has missing, duplicate, or invalid indexes.")
                if not isinstance(vector, list) or not vector or any(
                    type(v) not in (int, float) or not math.isfinite(v) for v in vector
                ) or not any(vector):
                    raise EmbeddingError("Embedding response contains an invalid vector.")
                expected_dimensions = expected_dimensions or len(vector)
                if len(vector) != expected_dimensions:
                    raise EmbeddingError("Embedding response has inconsistent dimensions.")
                ordered[index] = vector
            vectors.extend(ordered)
            usage = response.get("usage", {})
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if type(value) in (int, float):
                        self.last_usage[key] = self.last_usage.get(key, 0) + value
        return vectors


def main() -> None:
    parser = argparse.ArgumentParser(description="Test OpenRouter embeddings; makes a paid API request.")
    parser.add_argument("--text", action="append", required=True, help="Repeat for multiple inputs.")
    parser.add_argument("--dimensions", type=int)
    args = parser.parse_args()
    try:
        client = OpenRouterEmbeddings(dimensions=args.dimensions)
        vectors = client.embed(args.text)
    except (EmbeddingError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"model": client.model, "inputs": len(vectors),
                      "dimensions": len(vectors[0]), "usage": client.last_usage}, indent=2))


if __name__ == "__main__":
    main()
