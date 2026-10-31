import sqlite3
import pytest

from aerotrace.config import settings
from aerotrace.engine import AeroTrace
from aerotrace.revision import resolve


pytestmark = pytest.mark.skipif(
    not settings.database.exists() or not settings.vectors.exists(),
    reason="Build the local corpus index to run integration tests",
)


def test_all_multiversion_dockets_follow_publication_dates():
    with sqlite3.connect(settings.database) as con:
        con.row_factory = sqlite3.Row
        dockets = [row[0] for row in con.execute(
            "SELECT docket_number FROM notices GROUP BY docket_number HAVING count(*) > 1"
        )]
        assert len(dockets) == 99
        for docket in dockets:
            result = resolve(con, "", {"docket": docket})
            assert not result.get("error"), docket
            history = result["history"]
            assert result["revision"]["version_count"] == len(history)
            assert result["revision"]["latest_date"] == max(row["publish_date"] for row in history)
            assert sum(row["corpus_status"] == "latest" for row in history) == 1


def test_latest_and_explicit_old_version_keep_evidence_in_one_record():
    if not settings.openrouter_key:
        pytest.skip("OPENAI_API_KEY is needed for the engine integration test")
    engine = AeroTrace()
    for requested, expected in (("", "NPRM-011"), ("NPRM-121", "NPRM-121")):
        result = engine.ask("What does FAA-2026-1338 propose?", record_id=requested,
                            keyword_only=True, use_llm=False)
        assert result["notice"]["record_id"] == expected
        assert {item["record_id"] for item in result["evidence"]} == {expected}


def test_conflicting_version_identifiers_refuse():
    if not settings.openrouter_key:
        pytest.skip("OPENAI_API_KEY is needed for the engine integration test")
    engine = AeroTrace()
    result = engine.ask("What does FAA-2026-1338 version 2 propose?", record_id="NPRM-121",
                        keyword_only=True, use_llm=False)
    assert result["status"] == "REFUSED"
    assert result["reason_code"] == "CONFLICTING_VERSION"
