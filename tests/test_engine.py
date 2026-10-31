import pytest

from aerotrace.engine import get_engine


@pytest.fixture(scope="module")
def e():
    return get_engine()


@pytest.mark.parametrize("query,status,reason", [
    ("Does the R22 directive apply to serial number 1200?", "ANSWERED", None),
    ("Does the R22 directive apply to my helicopter?", "REFUSED", "MISSING_CONFIG"),
    ("What does FAA-2099-12345 propose?", "REFUSED", "OUT_OF_SCOPE"),
    ("What is the MR servo inspection?", "REFUSED", "AMBIGUOUS_TERM"),
    ("What is the recipe for chocolate cake?", "REFUSED", "NO_EVIDENCE"),
    ("What does the revised proposal FAA-2026-1338 require for A330?", "REFUSED", "CONFLICT"),
])
def test_status(e, query, status, reason):
    r = e.ask(query)
    assert (r["status"], r["reason_code"]) == (status, reason)


def test_every_statement_is_cited_and_verified(e):
    r = e.ask("What inspection is proposed for the main rotor servo-control upper ball bearing end on Airbus Helicopters?")
    assert r["statements"]
    for s in r["statements"]:
        assert s["verification"]["verified"]
        c = s["citations"][0]
        assert c["document_id"] and c["paragraph"] and s["text"].lower()[:40] in c["source_text"].lower()


def test_withdrawn_flagged(e):
    r = e.ask("Does the R22 directive apply to serial number 1200?")
    assert r["revision_status"]["withdrawn"] and not r["revision_status"]["is_latest"]


def test_latency_under_3s(e):
    assert e.ask("Boeing 787-8 LFCD split frames inspection")["latency_ms"] < 3000
