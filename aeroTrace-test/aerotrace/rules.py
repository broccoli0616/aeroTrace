from __future__ import annotations

import re


RANGE_RE = re.compile(
    r"(?:serial numbers?|S/Ns?|MSNs?)\s*(?:\([^)]*\)\s*)?(?:of\s+)?"
    r"([A-Z0-9](?:[A-Z0-9./-]*[A-Z0-9])?)\s+(?:through|thru|to)\s+"
    r"([A-Z0-9](?:[A-Z0-9./-]*[A-Z0-9])?)",
    re.I,
)


def _norm(value: str) -> str:
    return re.sub(r"[\s-]+", "-", value.upper()).strip("-")


def _serial_key(value: str) -> tuple[str, int, str] | None:
    match = re.fullmatch(r"([A-Z]*[-/]?)(\d+)([A-Z/]*)", value.upper().replace(" ", ""))
    if not match:
        return None
    return match.group(1).rstrip("-/"), int(match.group(2)), match.group(3)


def evaluate_applicability(notice: dict, applicability_chunks: list[dict], config: dict) -> dict:
    source = "\n".join(c["text"] for c in applicability_chunks)
    refs = [c["chunk_id"] for c in applicability_chunks]
    model = config.get("model")
    serial = config.get("serial_number")
    result = {"result": "NOT_EVALUATED", "checks": [], "missing": [], "source_chunk_ids": refs}
    if not model and not serial:
        return result
    if not source:
        return {**result, "result": "UNKNOWN", "missing": ["applicability section"],
                "reason": "The official notice did not yield an identifiable applicability section."}
    if not model:
        return {**result, "result": "UNKNOWN", "missing": ["model"],
                "reason": "A model is required to evaluate applicability."}
    listed = [p.strip() for p in str(notice.get("model") or "").split("|") if p.strip()]
    source_norm = _norm(source)
    model_norm = _norm(model)
    model_match = any(model_norm == _norm(item) or model_norm.startswith(_norm(item) + "-") for item in listed)
    if not listed:
        model_match = model_norm in source_norm
    if not model_match:
        return {**result, "result": "DOES_NOT_APPLY",
                "reason": f"Model {model} is not identified in the applicability evidence.",
                "checks": [{"field": "model", "result": "failed", "value": model}]}
    result["checks"].append({"field": "model", "result": "passed", "value": model})
    ranges = list(RANGE_RE.finditer(source))
    if ranges:
        if not serial:
            return {**result, "result": "UNKNOWN", "missing": ["serial_number"],
                    "reason": "The applicability evidence contains serial-number ranges."}
        key = _serial_key(serial)
        if key is None:
            return {**result, "result": "UNKNOWN", "missing": ["parseable serial_number"],
                    "reason": f"Serial {serial} could not be compared safely."}
        matched = False
        spans = []
        for match in ranges:
            lo, hi = _serial_key(match.group(1)), _serial_key(match.group(2))
            spans.append(f"{match.group(1)} through {match.group(2)}")
            if lo and hi and key[0] == lo[0] == hi[0] and lo[1] <= key[1] <= hi[1]:
                matched = True
        if not matched:
            return {**result, "result": "DOES_NOT_APPLY",
                    "reason": f"Serial {serial} is outside the extracted range(s): {', '.join(spans)}.",
                    "checks": result["checks"] + [{"field": "serial_number", "result": "failed", "value": serial}]}
        result["checks"].append({"field": "serial_number", "result": "passed", "value": serial})
    if re.search(r"\bcertain\b|specified in .{0,80}(?:service bulletin|MCAI)", source, re.I) and not ranges:
        return {**result, "result": "UNKNOWN",
                "reason": "The notice refers to additional effectivity information that was not safely parsed."}
    return {**result, "result": "APPLIES", "reason": "All extracted applicability conditions passed."}

