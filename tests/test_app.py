"""The Streamlit UI, driven headlessly: upload -> review -> confirm -> ask -> remove."""
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import aerotrace.engine
from test_uploads import PAGE1, PAGE2, make_pdf


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("AEROTRACE_UPLOAD_DIR", str(tmp_path))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setattr(aerotrace.engine, "_ENGINE", None)  # fresh engine on the temp upload folder
    st.cache_resource.clear()
    at = AppTest.from_file("../app.py", default_timeout=120).run()
    yield at
    st.cache_resource.clear()
    aerotrace.engine._ENGINE = None


def test_rejects_unsupported_content(app):
    app.file_uploader[0].upload("report.pdf", b"MZ this is not a pdf").run()
    assert not app.exception
    assert "not a PDF" in app.error[0].value


def test_upload_review_confirm_ask_remove(app):
    app.file_uploader[0].upload("zephyr.pdf", make_pdf([PAGE1, PAGE2]), "application/pdf").run()
    assert not app.exception
    assert app.subheader[0].value == "Review: zephyr.pdf"
    assert app.text_input(key="kupload/zephyr").value == "FAA-2099-0001"   # detected docket
    assert "ZX-100" in app.text_input(key="oupload/zephyr").value          # detected models

    app.button[[b.label for b in app.button].index("✅ Confirm & add")].click().run()
    assert not app.exception
    assert any("Added zephyr.pdf" in s.value for s in app.sidebar.success)

    app.text_input(key="q").input("Does FAA-2099-0001 apply to Model ZX-100 serial number 0150?").run()
    assert [m.value for m in app.metric if m.label == "Applicability"] == ["APPLIES"]
    assert any("uploaded, not verified" in m.value for m in app.markdown)

    app.button(key=next(b.key for b in app.sidebar.button if b.key and b.key.startswith("del"))).click().run()
    assert any("Removed zephyr.pdf" in i.value for i in app.sidebar.info)


def test_password_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    monkeypatch.setenv("AEROTRACE_UPLOAD_DIR", str(tmp_path))
    st.cache_resource.clear()
    at = AppTest.from_file("../app.py", default_timeout=120).run()
    assert not at.exception
    assert len(at.text_input) == 1 and at.text_input[0].label == "Password"  # nothing else is rendered
    assert not at.sidebar.header

    at.text_input[0].input("wrong")  # form: value and submit go in the same run
    at.button[0].click().run()
    assert at.error[0].value == "Wrong password." and not at.sidebar.header

    at.text_input[0].input("s3cret")  # form: value and submit go in the same run
    at.button[0].click().run()
    assert not at.exception
    assert "Add documents" in " ".join(h.value for h in at.sidebar.header)
    st.cache_resource.clear()
