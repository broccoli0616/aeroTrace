import pytest

from aerotrace import compose
from aerotrace.engine import get_engine
from aerotrace.text import tokenize


@pytest.mark.parametrize("question", [
    "What does docket FAA-2026-11814 cover?",
    "What does FAA-2026-11814 cover?",
    "What topics are covered by docket FAA-2026-11814?",
    "What does record NPRM-001 cover?",
])
def test_overview_words_do_not_require_literal_repetition(question):
    ignore = set(tokenize("FAA-2026-11814 NPRM-001"))
    statements = [{"text": "The FAA proposes an inspection of the servo-control upper ball bearing end."}]
    score, missing = compose.coverage(question, statements, {}, ignore)
    assert score == 1 and missing == []


@pytest.mark.parametrize("question", [
    "What does FAA-2026-11814 say about the bearing cover?",
    "What inspection is required for the bearing cover in docket FAA-2026-11814?",
    "What is the torque on the engine cover?",
])
def test_physical_cover_remains_a_required_topic(question):
    assert "cover" in compose.topic_terms(question, set(tokenize("FAA-2026-11814")))


@pytest.mark.parametrize("question", [
    "What does docket FAA-2026-11814 cover?",
    "What does NPRM-001 cover?",
    "What does record NPRM-001 cover?",
])
def test_extractive_overview_questions_return_cited_answers(question):
    result = get_engine().ask(question)
    assert result["status"] == "ANSWERED"
    assert result["coverage_check"]["score"] == 1
    assert all(s["verification"]["verified"] for s in result["statements"])


def test_requested_but_unsupported_numeric_topic_still_refuses():
    result = get_engine().ask("What torque value does docket FAA-2026-11814 cover?")
    assert result["status"] == "REFUSED" and result["reason_code"] == "NO_EVIDENCE"
