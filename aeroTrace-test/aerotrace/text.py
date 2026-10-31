from __future__ import annotations

import hashlib
import re


TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+)*")


def clean(value: str | None) -> str:
    text = str(value or "").replace("\\n", "\n").replace("\\t", "\t")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tokens(value: str) -> list[str]:
    return [m.group(0).lower() for m in TOKEN_RE.finditer(value)]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def numbers(value: str) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z])\d+(?:[.,]\d+)*(?![A-Za-z])", value))

