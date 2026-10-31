from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    workbook: Path = ROOT / "challenge4_ad_index.xlsx"
    artifacts: Path = ROOT / "artifacts"
    raw_dir: Path = ROOT / "data" / "raw"
    database: Path = ROOT / "artifacts" / "index.sqlite"
    vectors: Path = ROOT / "artifacts" / "embeddings.npy"
    vector_manifest: Path = ROOT / "artifacts" / "embedding_manifest.json"
    openrouter_key: str = os.getenv("OPENAI_API_KEY", "")
    anthropic_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    embed_model: str = os.getenv("AEROTRACE_EMBED_MODEL", "text-embedding-3-small")
    llm_model: str = os.getenv("AEROTRACE_LLM_MODEL", "claude-sonnet-5-5")
    openrouter_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

    @property
    def embed_model_slug(self) -> str:
        return self.embed_model if "/" in self.embed_model else f"openai/{self.embed_model}"

    def ensure_dirs(self) -> None:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()

