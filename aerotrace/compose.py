"""Answer composition. Two modes:

- extractive (default, no API key needed): picks the best-matching sentences verbatim from retrieved passages.
- llm: Claude writes the answer, but may only use the passages it is given and must cite one per sentence.

Either way, every sentence then goes through verify.check(); the LLM is never trusted on its own.
"""
import json
import math
import os

from .text import sentences, tokenize

SYSTEM_PROMPT = """You answer questions for aircraft mechanics using ONLY the numbered source passages provided.
Rules:
- Every statement must be a direct quote or a very close paraphrase of ONE passage, and must cite that passage_id.
- Copy numbers, part numbers, model names, intervals and dates exactly as written in the passage.
- Do not use outside knowledge, do not combine facts from different documents into one sentence, and do not guess.
- If the passages do not answer the question, return answerable=false and no statements.
- Keep it short: at most 4 statements."""

# Words that say *what kind* of question it is, not *what it is about*. Ignored when matching and in coverage checks.
GENERIC_TERMS = set(tokenize(
    "ad ads directive directives airworthiness proposal proposals proposed propose proposes rule rules rulemaking "
    "nprm faa notice document require requires required requirement requirements what which latest current newest "
    "revision revised version apply applies applicable aircraft airplane helicopter engine serial number model "
    "say says tell summary summarize explain new existing"))


def coverage(query: str, statements: list[dict], idf: dict, ignore: set[str]) -> tuple[float, list[str]]:
    """Share (idf-weighted) of the question's topic words that appear in the answer, plus the missing words."""
    terms = set(tokenize(query)) - GENERIC_TERMS - ignore
    if not terms:
        return 1.0, []
    found = set(tokenize(" ".join(s["text"] for s in statements)))
    total = sum(idf.get(t, 1.0) for t in terms)
    got = sum(idf.get(t, 1.0) for t in terms if t in found)
    return got / total, sorted(t for t in terms if t not in found)


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "statements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "passage_id": {"type": "string"}},
                "required": ["text", "passage_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answerable", "statements"],
    "additionalProperties": False,
}


def extractive(query: str, passages: list[dict], idf: dict, max_sentences: int = 3,
               fallback_summary: bool = False, ignore: set[str] = frozenset()) -> list[dict]:
    """`ignore`: words that only identify the document (docket, maker, model) and say nothing about the topic."""
    q = set(tokenize(query)) - GENERIC_TERMS - ignore
    pool = [(rank, pos, s, p["passage_id"], set(tokenize(s)))
            for rank, p in enumerate(passages) for pos, s in enumerate(sentences(p["text"]))]
    # Greedy pick: each next sentence is rewarded mostly for question words not yet covered.
    scored, remaining = [], set(q)
    for _ in range(max_sentences):
        def gain(item):
            toks = item[4]
            return (sum(idf.get(t, 0) for t in remaining & toks) + 0.3 * sum(idf.get(t, 0) for t in q & toks)) \
                / math.sqrt(len(toks) + 5)
        cands = [it for it in pool if it not in scored]
        best = max(cands, key=gain, default=None)
        if best is None or gain(best) <= 0 or not (remaining & best[4] or not scored):
            break
        scored.append(best)
        remaining -= best[4]
    if not scored and (fallback_summary or not q):
        # Generic question about a named document ("what does FAA-2026-11814 propose?"): lead with its summary.
        summ = next((p for p in passages if p["paragraph"] == "summary"), None)
        if summ:
            return [{"text": s, "passage_id": summ["passage_id"]} for s in sentences(summ["text"])[:max_sentences]]
    scored.sort(key=lambda x: (x[0], x[1]))  # read in document order
    return [{"text": s, "passage_id": pid} for _, _, s, pid, _ in scored]


def llm(query: str, passages: list[dict]) -> tuple[list[dict], str | None]:
    """Returns (statements, error). On any API problem returns ([], error) so the engine can fall back."""
    try:
        import anthropic
    except ImportError:
        return [], "anthropic package not installed"
    sources = "\n\n".join(f"[passage_id: {p['passage_id']}]\n{p['text'][:2500]}" for p in passages)
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=os.getenv("AEROTRACE_MODEL", "claude-opus-5-5"),
            max_tokens=2000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
            messages=[{"role": "user", "content": f"SOURCE PASSAGES:\n{sources}\n\nQUESTION: {query}"}],
        )
    except anthropic.RateLimitError as e:
        return [], f"rate limited: {e}"
    except anthropic.APIStatusError as e:
        return [], f"API error {e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        return [], f"connection error: {e}"
    if response.stop_reason == "refusal":
        return [], "model declined the request"
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], "model returned invalid JSON"
    return (data["statements"] if data.get("answerable") else []), None
