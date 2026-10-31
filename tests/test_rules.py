"""The challenge's own example: Directive A applies only to serial numbers S-1000 to S-1500."""
from aerotrace.rules import evaluate, model_match
from aerotrace.verify import check

RULE = {
    "doc_id": "DIRECTIVE-A", "applies_to_models": ["X-100"], "scope": "certain",
    "serial_ranges": [{"model": None, "start": "S-1000", "end": "S-1500"}], "part_numbers": [],
    "mod_condition": None, "effective_date": None, "source_ref": "DIRECTIVE-A#(c)",
    "source_text": "This AD applies to Model X-100 airplanes, serial numbers S-1000 through S-1500.", "status": "PARSED",
}


def test_serial_inside_range_applies():
    assert evaluate(RULE, {"model": "X-100", "serial_number": "S-1200"})["result"] == "APPLIES"


def test_serial_outside_range_does_not_apply():
    r = evaluate(RULE, {"model": "X-100", "serial_number": "S-1600"})
    assert r["result"] == "DOES_NOT_APPLY" and "S-1600" in r["failed_condition"]


def test_missing_serial_is_unknown_never_assumed():
    r = evaluate(RULE, {"model": "X-100"})
    assert r["result"] == "UNKNOWN" and r["missing_fields"] == ["serial_number"]


def test_range_bounds_are_inclusive():
    assert evaluate(RULE, {"model": "X-100", "serial_number": "S-1500"})["result"] == "APPLIES"
    assert evaluate(RULE, {"model": "X-100", "serial_number": "S-1000"})["result"] == "APPLIES"


def test_unparsed_rule_refuses():
    assert evaluate({**RULE, "status": "UNPARSED"}, {"model": "X-100", "serial_number": "S-1200"})["result"] == "UNPARSED"


def test_model_matching():
    assert model_match("A330-243", "A330") == "covered"
    assert model_match("A330", "A330-200") == "partial"
    assert model_match("777X", "777") == "none"
    assert model_match("G150", "Gulfstream G150") == "exact"


def test_verifier_rejects_changed_numbers():
    src = "Torque the nut to 45 inch-pounds within 100 flight hours."
    assert check("Torque the nut to 45 inch-pounds within 100 flight hours.", src)["verified"]
    assert not check("Torque the nut to 54 inch-pounds within 100 flight hours.", src)["verified"]
