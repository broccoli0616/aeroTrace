"""Project configuration, including refreshed Claude credentials and model settings."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    load_dotenv(ROOT / ".env", override=False)


def project_openrouter_alias() -> str:
    """Read a project OpenRouter alias when a global direct OpenAI key masks it.

    This never overwrites OPENAI_API_KEY in the calling process.
    """
    value = (dotenv_values(ROOT / ".env").get("OPENAI_API_KEY") or "").strip()
    return value if value.startswith("sk-or-") else ""


@dataclass(frozen=True)
class ClaudeConfig:
    api_key: str = field(repr=False)
    model: str = "claude-sonnet-5-5"


def claude_config() -> ClaudeConfig:
    """Read the current project settings on each call, including edits to .env.

    For Claude, an explicit project setting wins over inherited shell settings.
    This avoids using a stale process key after editing the project's .env.
    """
    values = dotenv_values(ROOT / ".env")
    key = (values.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or "").strip()
    model = (values.get("AEROTRACE_MODEL") or values.get("ANTHROPIC_MODEL")
             or os.getenv("AEROTRACE_MODEL") or os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5-5").strip()
    return ClaudeConfig(key, model)
