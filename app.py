"""AeroTrace demo UI. Run: streamlit run app.py"""
import html
import json
import os
import re

import streamlit as st

from aerotrace.engine import get_engine

st.set_page_config(page_title="AeroTrace", page_icon="✈️", layout="wide")


@st.cache_resource
def engine():
    return get_engine()


EXAMPLES = [
    "Does the R22 directive apply to serial number 1200?",
    "Does the R22 directive apply to serial number 2600?",
    "Does the R22 directive apply to my helicopter?",
    "What inspection is proposed for the main rotor servo-control upper ball bearing end on Airbus Helicopters?",
    "What is the comment due date for FAA-2026-11814?",
    "What does the revised proposal FAA-2026-1338 require for A330?",
    "What is the torque for the Boeing 777X flap track?",
    "What is the MR servo inspection?",
]

st.title("✈️ AeroTrace")
st.caption("Traceable, context-aware search over FAA proposed Airworthiness Directives. "
           "Every sentence is cited and verified; uncertain questions are refused with a reason code.")

with st.sidebar:
    st.header("Aircraft configuration")
    st.caption("Optional. Overrides anything detected in the question.")
    model = st.text_input("Model", placeholder="e.g. R22, AS350B3, A330-243")
    serial = st.text_input("Serial number", placeholder="e.g. 1200")
    part = st.text_input("Installed part number", placeholder="e.g. 3G6410A00131")
    st.header("Answer mode")
    has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    use_llm = st.toggle("Let Claude write the answer", value=False, disabled=not has_key,
                        help="Claude only rewrites retrieved passages; every sentence is still verified. "
                             "Needs ANTHROPIC_API_KEY." if has_key else "Set ANTHROPIC_API_KEY to enable.")
    st.header("Try an example")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state["q"] = ex

query = st.text_input("Ask a question", key="q", placeholder="Does the R22 directive apply to serial number 1200?")
if not query:
    st.info(f"Corpus: {len(engine().docs)} FAA AD NPRM records, {len(engine().passages)} citable passages.")
    st.stop()

res = engine().ask(query, model=model or None, serial_number=serial or None, part_number=part or None,
                   mode="llm" if use_llm else "extractive")

tab_view, tab_json = st.tabs(["Answer", "JSON"])
with tab_view:
    if res["status"] == "ANSWERED":
        st.success(f"**ANSWERED**{(' - ' + res['message']) if res['message'] else ''}  ·  {res['latency_ms']} ms")
    else:
        st.error(f"**REFUSED · {res['reason_code']}**  ·  {res['latency_ms']} ms\n\n{res['message']}")

    cfg = {k: v for k, v in res["configuration"].items() if v}
    c1, c2, c3 = st.columns(3)
    c1.metric("Configuration", ", ".join(f"{k}={v}" for k, v in cfg.items()) or "none given")
    appl = res["applicability"] or {}
    c2.metric("Applicability", appl.get("result", "-"))
    rev = res["revision_status"] or {}
    c3.metric("Latest revision?", {True: "Yes", False: "No"}.get(rev.get("is_latest"), "-"))

    if appl.get("failed_condition"):
        st.warning(f"**Applicability detail:** {appl['failed_condition']}")
    if appl.get("checks"):
        st.caption("Checks passed: " + "; ".join(appl["checks"]))

    for w in res["warnings"]:
        st.warning(w)

    if res["statements"]:
        st.subheader("Answer")
        for n, s in enumerate(res["statements"], start=1):
            c = s["citations"][0]
            st.markdown(f"**{n}.** {s['text']}  \n"
                        f"<small>📄 `{c['docket']}` · {c['revision']} · {c['page']} · ¶ `{c['paragraph']}` · "
                        f"verified: {s['verification']['reason']}</small>", unsafe_allow_html=True)
            with st.expander("Show highlighted source passage"):
                src = html.escape(c["source_text"])
                quote = html.escape(s["text"])
                src = re.sub(re.escape(quote), f"<mark>{quote}</mark>", src, count=1) if quote in src else src
                st.markdown(f"<div style='font-size:0.9em'>{src}</div>", unsafe_allow_html=True)
                if c["url"]:
                    st.markdown(f"[Open official Federal Register notice]({c['url']})")
                st.caption(c["fr_citation"])

    if res["removed_statements"]:
        with st.expander(f"{len(res['removed_statements'])} sentence(s) removed by the verifier"):
            st.json(res["removed_statements"])

    if res["candidates"]:
        with st.expander("Candidate documents considered"):
            st.table(res["candidates"])
    if rev:
        with st.expander("Revision history"):
            st.json(rev)
    st.caption("⚠️ " + res["disclaimer"])

with tab_json:
    st.json(res)
    st.download_button("Download audit log (JSON)", json.dumps(res, indent=2, default=str),
                       file_name="aerotrace_audit.json", mime="application/json")
