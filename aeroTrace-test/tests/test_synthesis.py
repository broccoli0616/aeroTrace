from aerotrace.config import Settings
from aerotrace import synthesize as synthesis
from aerotrace.synthesize import validate_answer
from aerotrace.text import numbers


EVIDENCE = [{"chunk_id": "c1", "text": "Inspect the component every 25 flight hours."}]


def test_citation_validator_accepts_grounded_claim():
    answer = {"status": "ANSWERED", "reason": None, "statements": [
        {"text": "Inspect every 25 flight hours.", "passage_id": "c1", "evidence_quote": "every 25 flight hours"}
    ]}
    assert validate_answer(answer, EVIDENCE)["status"] == "ANSWERED"


def test_citation_validator_rejects_invented_number():
    answer = {"status": "ANSWERED", "reason": None, "statements": [
        {"text": "Inspect every 50 flight hours.", "passage_id": "c1", "evidence_quote": "every 25 flight hours"}
    ]}
    checked = validate_answer(answer, EVIDENCE)
    assert checked["status"] == "REFUSED"
    assert checked["removed_statements"]


def test_citation_validator_accepts_number_from_verified_checks():
    answer = {"status": "ANSWERED", "reason": None, "statements": [
        {"text": "Model A330-941 is not included.", "passage_id": "c1",
         "evidence_quote": "Inspect the component"}
    ]}
    checked = validate_answer(answer, EVIDENCE, allowed_numbers=numbers("A330-941"))
    assert checked["status"] == "ANSWERED"


def test_synthesized_refusal_cannot_be_reversed(monkeypatch):
    monkeypatch.setattr(synthesis, "_request", lambda *args, **kwargs: {
        "status": "ANSWERED",
        "reason": None,
        "statements": [{"text": "Unsupported answer", "passage_id": "x", "evidence_quote": "x"}],
    })
    result = synthesis.synthesize_refusal(
        Settings(),
        "Does this apply?",
        reason_code="MISSING_CONFIG",
        verified_reason="The serial number is required.",
    )
    assert result["status"] == "REFUSED"
    assert result["reason"] == "The serial number is required."
    assert result["statements"] == []


def test_synthesized_refusal_uses_model_explanation(monkeypatch):
    monkeypatch.setattr(synthesis, "_request", lambda *args, **kwargs: {
        "status": "REFUSED",
        "reason": "I need the aircraft model and serial number before I can determine applicability.",
        "statements": [],
    })
    result = synthesis.synthesize_refusal(
        Settings(),
        "Does this apply to my aircraft?",
        reason_code="MISSING_CONFIG",
        verified_reason="More aircraft configuration is required.",
    )
    assert result["status"] == "REFUSED"
    assert "model and serial number" in result["reason"]


def test_non_applicability_answer_must_state_result_directly(monkeypatch):
    monkeypatch.setattr(synthesis, "_request", lambda *args, **kwargs: {
        "status": "ANSWERED",
        "reason": None,
        "statements": [{
            "text": "The proposal lists several covered models.",
            "passage_id": "c1",
            "evidence_quote": "Inspect the component",
        }],
    })
    result = synthesis.synthesize(
        Settings(),
        "Does this apply?",
        {"applicability": {"result": "DOES_NOT_APPLY"}},
        EVIDENCE,
    )
    assert result["status"] == "REFUSED"
    assert not result["statements"]

