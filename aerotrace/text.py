"""Small text helpers shared by ingestion, retrieval and verification."""
import re

STOPWORDS = set("""
a an and are as at be been but by can could did do does for from had has have how i if in into is it its
may me must my no not of on or our should so than that the their them then there these they this those to
was we were what when where which who why will with would you your about any all also each other such
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-/.][a-z0-9]+)*")
_NUMBER_RE = re.compile(r"\d+(?:[.,/-]\d+)*")
_SENT_RE = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")


def clean(text: str) -> str:
    """Turn the TSV's literal '\\n' / '\\t' escapes into whitespace and squash runs of spaces."""
    text = text.replace("\\n", "\n").replace("\\t", " ").replace("\t", " ")
    text = re.sub(r"[  ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def stem(t: str) -> str:
    """Tiny suffix stripper so 'proposes', 'proposed', 'proposing' all match 'propose'."""
    if len(t) <= 3 or any(c.isdigit() for c in t):
        return t
    for suf in ("ments", "ment", "ations", "ation", "ions", "ion", "ing", "ed", "es"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)].rstrip("e")
    if t.endswith("s") and not t.endswith("ss"):  # "hmus" -> "hmu", "seals" -> "seal"
        t = t[:-1]
    return t.rstrip("e") if len(t) > 4 else t


def tokenize(text: str, keep_stopwords: bool = False) -> list[str]:
    toks = _TOKEN_RE.findall(text.lower())
    return [stem(t) for t in toks if keep_stopwords or t not in STOPWORDS]


def numbers(text: str) -> set[str]:
    """All numeric tokens (torque values, intervals, part numbers...). Used by the verifier."""
    return {n.strip(".,") for n in _NUMBER_RE.findall(text)}


def sentences(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        out.extend(s.strip() for s in _SENT_RE.split(line) if len(s.strip()) > 20)
    return out


def norm_docket(docket: str) -> str:
    """Group key for revisions: 'FAA-2022-1308R1', 'FAA-2025-0199_S', 'FAA-2009-0555-W' share a base docket."""
    d = docket.strip().upper().replace("FAA RULES ", "").replace("FAA ", "FAA-")
    d = re.sub(r"(R\d+|_S|-W)$", "", d)
    return d
