"""Applicability rules: extract a fixed-schema rule per document, then evaluate it deterministically.

No LLM is involved here. If the text can't be parsed with confidence, the rule is marked UNPARSED
and the engine refuses to give an applicability answer instead of guessing.
"""
import re

_SER = r"(?:manufacturer serial numbers?|serial numbers?|S/ ?Ns?|MSNs?)\s*(?:\([^)]{1,8}\)\s*)?(?:of\s+)?"
_RANGE_RE = re.compile(_SER + r"([A-Z0-9][\w/.]*(?:-[\w/.]+)?)\s+(?:through|thru|to)\s+([A-Z0-9][\w/.]*(?:-[\w/.]+)?)", re.I)
_OPEN_RE = re.compile(_SER + r"([A-Z0-9][\w/.]*(?:-[\w/.]+)?)\s+(?:and (?:subsequent|on|up)|or (?:higher|later|greater))", re.I)
_MODEL_RE = re.compile(
    r"\bModels?\s+(.+?)(?=\s+(?:series\s+)?(?:airplanes?|helicopters?|engines?|propellers?|gliders?|rotorcraft|"
    r"turbofan|turboprop|turboshaft|aircraft|powered|that|which|with|having|equipped|certificated)\b|[;:(]|\.\s|$)",
    re.I,
)
_PN_RE = re.compile(r"(?:part numbers?|P/Ns?)\)?\s*(?:\(P/Ns?\)\s*)?((?:[A-Z0-9][\w-]{3,}(?:\s*,\s*|\s+or\s+|\s+and\s+|,\s+or\s+)?)+)", re.I)
_MOD_RE = re.compile(r"\b(modification|pre-mod|post-mod|service bulletin|equipped with|having .{0,60}installed)\b", re.I)


def _models_from_text(text: str) -> list[str]:
    models = []
    for m in _MODEL_RE.finditer(text):
        seg = m.group(1)
        for part in re.split(r",\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+", seg):
            part = re.sub(r"\s+series$", "", part.strip(" ,"))
            if not part or not re.search(r"\d", part) or len(part) > 30:
                continue
            if part.startswith("-") and models:  # "737-800 and -900" -> "737-900"
                part = models[-1].split("-")[0] + part
            models.append(part)
    return models


def norm_model(m: str) -> str:
    m = re.sub(r"\b(MODELS?|SERIES)\b", " ", m.upper())
    return re.sub(r"[\s-]+", "-", m).strip("-")


def model_match(user: str, rule_model: str) -> str:
    """'exact' | 'covered' (rule is a series covering the user's variant) | 'partial' (user too vague) | 'none'."""
    u = norm_model(user)
    words = rule_model.split()
    # Also try the rule model without leading maker words: "Gulfstream G150" should match "G150".
    variants = [norm_model(" ".join(words[i:])) for i in range(len(words)) if re.search(r"\d", " ".join(words[i:]))]
    if not u:
        return "none"
    best = "none"
    for r in variants or [norm_model(rule_model)]:
        if not r:
            continue
        if u == r:
            return "exact"
        # A series covers its dash variants ("A330" covers "A330-243") but "777" does not cover "777X".
        if u.startswith(r) and u[len(r)] == "-":
            best = "covered"
        elif best == "none" and r.startswith(u) and not r[len(u)].isdigit():
            best = "partial"  # "EC135" vs "EC135P2": user must give the exact variant (never assumed)
    return best


def serial_key(s: str):
    digits = re.sub(r"\D", "", s or "")
    return int(digits) if digits else None


def _applicability_text(doc: dict, passages: list[dict]) -> tuple[str, str]:
    """Pick the best passage stating who the rule applies to. Returns (text, passage_id)."""
    for p in passages:
        if p["section"].lower() == "applicability":
            return p["text"], p["passage_id"]
    for p in passages:
        m = re.search(r"(Applicability:|This AD applies to)(.{0,900})", p["text"])
        if m and p["paragraph"] != "summary":
            return m.group(0), p["passage_id"]
    first = re.split(r"(?<=\.)\s", doc["summary"], maxsplit=1)[0] if doc["summary"] else ""
    return first, f"{doc['doc_id']}#{'summary' if doc['summary'] else 'metadata'}"


def extract_rule(doc: dict, passages: list[dict]) -> dict:
    text, ref = _applicability_text(doc, passages)
    text = re.sub(r"(\w)-\s+(\d)", r"\1-\2", text)  # "28- 7125001" -> "28-7125001", "EMB- 145LR" -> "EMB-145LR"
    text_models = _models_from_text(text)
    models = list(dict.fromkeys(doc["models"] + text_models))

    ranges = []
    for m in _RANGE_RE.finditer(text):
        before = text[max(0, m.start() - 120): m.start()]
        owner = _models_from_text(before[before.rfind("Model"):]) if "Model" in before else []
        ranges.append({"model": owner[-1] if owner else None, "start": m.group(1), "end": m.group(2)})
    for m in _OPEN_RE.finditer(text):
        ranges.append({"model": None, "start": m.group(1), "end": None})

    low = text.lower()
    scope = "all" if re.search(r"\b(for|to) all\b|all serial numbers", low) else (
        "certain" if re.search(r"\bcertain\b", low) else "unspecified")
    part_numbers = []
    for m in _PN_RE.finditer(text):
        part_numbers += [t for t in re.split(r"\s*,\s*|\s+or\s+|\s+and\s+", m.group(1)) if re.search(r"\d", t)]
    mod = _MOD_RE.search(text)

    if not models:
        status = "UNPARSED"
    elif (scope == "certain" and not ranges and not part_numbers) or (mod and not part_numbers):
        status = "PARTIAL"  # we know the models, but an extra condition exists that we could not parse
    else:
        status = "PARSED"

    return {
        "doc_id": doc["doc_id"],
        "applies_to_models": models,
        "scope": scope,
        "serial_ranges": ranges,
        "part_numbers": list(dict.fromkeys(part_numbers)),
        "mod_condition": mod.group(0) if mod else None,
        "effective_date": None,  # NPRMs are proposals: no effective date until a final rule is issued
        "source_ref": ref,
        "source_text": text[:600],
        "status": status,
    }


def evaluate(rule: dict, config: dict) -> dict:
    """Apply a rule to a user configuration {model, serial_number, part_number}. Never assumes defaults."""
    out = {"result": None, "failed_condition": None, "missing_fields": [], "checks": [], "rule_ref": rule["source_ref"],
           "rule_text": rule["source_text"]}

    def done(result, **kw):
        out.update(result=result, **kw)
        return out

    if rule["status"] == "UNPARSED":
        return done("UNPARSED", failed_condition="Could not parse which aircraft this rule covers.")

    model = (config.get("model") or "").strip()
    if not model:
        return done("UNKNOWN", missing_fields=["model"])
    matches = {m: model_match(model, m) for m in rule["applies_to_models"]}
    hit = [m for m, k in matches.items() if k in ("exact", "covered")]
    if not hit:
        partial = [m for m, k in matches.items() if k == "partial"]
        if partial:
            return done("UNKNOWN", missing_fields=["exact model variant"],
                        failed_condition=f"'{model}' is less specific than the listed models: {', '.join(partial[:8])}")
        return done("DOES_NOT_APPLY",
                    failed_condition=f"Model '{model}' is not in the applicability list: {', '.join(rule['applies_to_models'][:12])}")
    out["checks"].append(f"model {model} matches {hit[0]}")

    ranges = [r for r in rule["serial_ranges"] if r["model"] is None or model_match(model, r["model"]) != "none"]
    if ranges:
        sn = (config.get("serial_number") or "").strip()
        if not sn:
            return done("UNKNOWN", missing_fields=["serial_number"])
        key = serial_key(sn)
        if key is None:
            return done("UNPARSED", failed_condition=f"Serial number '{sn}' has no numeric part to compare.")
        for r in ranges:
            lo, hi = serial_key(r["start"]), serial_key(r["end"]) if r["end"] else None
            if lo is not None and key >= lo and (hi is None or key <= hi):
                out["checks"].append(f"serial {sn} within {r['start']} to {r['end'] or 'and higher'}")
                break
        else:
            spans = "; ".join(f"{r['start']} to {r['end'] or 'and higher'}" for r in ranges)
            return done("DOES_NOT_APPLY", failed_condition=f"Serial {sn} is outside the range(s): {spans}")

    if rule["part_numbers"]:
        pn = (config.get("part_number") or "").strip().upper()
        if not pn:
            return done("UNKNOWN", missing_fields=["part_number (installed part)"])
        if pn not in {p.upper() for p in rule["part_numbers"]}:
            return done("DOES_NOT_APPLY",
                        failed_condition=f"Part {pn} not in affected part numbers: {', '.join(rule['part_numbers'][:10])}")
        out["checks"].append(f"part {pn} is affected")

    if rule["status"] == "PARTIAL":
        return done("UNPARSED", failed_condition=(
            f"The rule covers {'certain' if rule['scope'] == 'certain' else 'some'} {hit[0]} aircraft"
            + (f" with condition '{rule['mod_condition']}'" if rule["mod_condition"] else "")
            + ", but the exact condition could not be parsed. Check the official notice."))
    return done("APPLIES")
