from aerotrace.rules import evaluate_applicability


NOTICE = {"model": "R22"}
CHUNKS = [{
    "chunk_id": "NPRM-X:0001",
    "text": "This AD applies to Model R22 helicopters, serial numbers 0002 through 2519.",
}]


def test_serial_inside_range_applies():
    result = evaluate_applicability(NOTICE, CHUNKS, {"model": "R22", "serial_number": "1200"})
    assert result["result"] == "APPLIES"


def test_serial_outside_range_does_not_apply():
    result = evaluate_applicability(NOTICE, CHUNKS, {"model": "R22", "serial_number": "2600"})
    assert result["result"] == "DOES_NOT_APPLY"


def test_missing_serial_is_unknown():
    result = evaluate_applicability(NOTICE, CHUNKS, {"model": "R22", "serial_number": None})
    assert result["result"] == "UNKNOWN"

