"""Deterministic citation checker. A sentence survives only if its cited passage really supports it."""
import re

from .text import numbers, tokenize

MIN_OVERLAP = 0.85


def _squash(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def check(statement: str, passage_text: str) -> dict:
    if _squash(statement) and _squash(statement) in _squash(passage_text):
        return {"verified": True, "overlap": 1.0, "reason": "exact quote"}
    missing_numbers = numbers(statement) - numbers(passage_text)
    if missing_numbers:
        # Wrong numbers are the most dangerous failure (torque, intervals, part numbers): reject outright.
        return {"verified": False, "overlap": 0.0, "reason": f"numbers not in source: {sorted(missing_numbers)}"}
    toks = tokenize(statement)
    if not toks:
        return {"verified": False, "overlap": 0.0, "reason": "empty statement"}
    src = set(tokenize(passage_text))
    overlap = sum(t in src for t in toks) / len(toks)
    ok = overlap >= MIN_OVERLAP
    return {"verified": ok, "overlap": round(overlap, 2),
            "reason": "high-overlap paraphrase" if ok else f"only {overlap:.0%} of words found in source"}
