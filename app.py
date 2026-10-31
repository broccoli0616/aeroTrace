"""AeroTrace demo UI. Run: streamlit run app.py"""
import html
import json
import os
import re

import streamlit as st

from aerotrace import rules
from aerotrace.engine import get_engine
from aerotrace.uploads import REVISION_CHOICES, UploadError, file_hash, parse

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
    st.header("📎 Add documents")
    st.caption("PDF, Word (.docx), Excel (.xlsx) or TSV, up to 50 MB. Other formats are rejected.")
    upload = st.file_uploader("Drop a file", type=["pdf", "docx", "xlsx", "tsv"],
                              key=f"uploader{st.session_state.get('uploader_n', 0)}")
    backend = engine().index.embed_backend
    if backend and not backend.startswith("sentence-transformers"):
        st.caption(f"🔒 Uploaded text is sent to {backend.split('/')[0]} to create embeddings.")
    if flash := st.session_state.pop("flash", None):
        getattr(st, flash[0])(flash[1])
    stored = engine().uploads.items() if engine().uploads else []
    if stored:
        st.caption("Uploaded documents (shared with everyone using this app):")
    for item in stored:
        c_name, c_del = st.columns([5, 1])
        c_name.markdown(f"📄 {item['file']}")
        if c_del.button("🗑", key=f"del{item['sha256']}", help="Remove this document"):
            n = engine().remove_upload(item["sha256"])
            st.session_state["flash"] = ("info", f"Removed {item['file']} ({n} document(s)).")
            st.rerun()
    for err in getattr(engine(), "upload_errors", []):
        st.warning(f"Could not reload {err}")

    st.header("Try an example")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state["q"] = ex



def reset_upload(kind: str, message: str):
    st.session_state.pop("pending", None)
    st.session_state["uploader_n"] = st.session_state.get("uploader_n", 0) + 1  # clears the drop zone
    st.session_state["flash"] = (kind, message)
    st.rerun()


# ---------- upload review: nothing becomes searchable until the user confirms what was extracted ----------
if upload is not None:
    data = upload.getvalue()
    sha = file_hash(data)
    pending = st.session_state.get("pending")
    if engine().uploads.find(sha):
        reset_upload("info", f"{upload.name} was already added.")
    if not pending or pending["sha256"] != sha:
        try:
            with st.spinner(f"Reading {upload.name}..."):
                pending = {**parse(upload.name, data), "name": upload.name}
            st.session_state["pending"] = pending
        except UploadError as e:
            reset_upload("error", f"❌ {upload.name}: {e}")

    st.subheader(f"Review: {pending['name']}")
    st.caption(f"{pending['kind']} · {len(pending['docs'])} document(s) · {len(pending['passages'])} citable passages. "
               "Check what was extracted, fix anything wrong, then add it.")
    for w in pending["warnings"]:
        st.warning(w)
    with st.form("review"):
        overrides = {}
        editable = len(pending["docs"]) <= 5
        if not editable:  # a whole spreadsheet of notices: show a summary instead of hundreds of forms
            st.dataframe([{"document": d["doc_id"], "docket": d["docket"], "title": d["title"],
                           "models": ", ".join(d["models"][:6]), "revision": d["revision"]} for d in pending["docs"]],
                         use_container_width=True)
        for d in pending["docs"] if editable else []:
            st.markdown(f"**{d['doc_id']}**")
            c1, c2 = st.columns(2)
            title = c1.text_input("Title", d["title"], key=f"t{d['doc_id']}")
            docket = c2.text_input("Docket number", d["docket"], key=f"k{d['doc_id']}",
                                   help="Documents sharing a docket are treated as revisions of each other.")
            make = c1.text_input("Make", d["make"], key=f"m{d['doc_id']}")
            models = c2.text_input("Models (comma-separated)", ", ".join(d["models"]), key=f"o{d['doc_id']}",
                                   help="Applicability checks only work when the models are right.")
            rev_options = list(dict.fromkeys([d["revision"]] + REVISION_CHOICES))
            revision = c1.selectbox("Revision", rev_options, key=f"r{d['doc_id']}")
            overrides[d["doc_id"]] = {"title": title, "docket": docket, "make": make,
                                      "models": models.split(","), "revision": revision}
            rule = rules.extract_rule(d, [p for p in pending["passages"] if p["doc_id"] == d["doc_id"]])
            ranges = ", ".join(f"{r['start']}–{r['end'] or 'on'}" for r in rule["serial_ranges"]) or "none found"
            c2.caption(f"Detected applicability: **{rule['status']}** · serial ranges: {ranges}")
            if rule["status"] == "UNPARSED":
                st.warning("No applicability rule was found: 'does this apply to my aircraft?' questions about this "
                           "document will be refused. Add the models above if you know them.")
        c_ok, c_no = st.columns(2)
        confirm = c_ok.form_submit_button("✅ Confirm & add", type="primary", use_container_width=True)
        discard = c_no.form_submit_button("Discard", use_container_width=True)
    if discard:
        reset_upload("info", f"Discarded {pending['name']}.")
    if confirm:
        try:
            with st.spinner("Indexing and embedding..."):
                out = engine().add_upload(pending["name"], data, overrides)
        except UploadError as e:
            reset_upload("error", f"❌ {pending['name']}: {e}")
        if out["status"] == "added":
            extra = f" Skipped {len(out['skipped'])} already in the corpus." if out["skipped"] else ""
            reset_upload("success", f"✅ Added {pending['name']}: {len(out['docs'])} document(s) are now searchable.{extra}")
        elif out["status"] == "duplicate":
            reset_upload("warning", f"{pending['name']} only contains documents that are already in the corpus.")
        else:
            reset_upload("info", f"{pending['name']} was already added.")
    st.stop()

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
            badge = "📎 <b>uploaded, not verified against the Federal Register</b> · " if c.get("uploaded") else ""
            st.markdown(f"**{n}.** {s['text']}  \n"
                        f"<small>{badge}📄 `{c['docket']}` · {c['revision']} · {c['page']} · ¶ `{c['paragraph']}` · "
                        f"verified: {s['verification']['reason']}</small>", unsafe_allow_html=True)
            with st.expander("Show highlighted source passage"):
                src = html.escape(c["source_text"])
                quote = html.escape(s["text"])
                src = re.sub(re.escape(quote), f"<mark>{quote}</mark>", src, count=1) if quote in src else src
                st.markdown(f"<div style='font-size:0.9em'>{src}</div>", unsafe_allow_html=True)
                if c["url"] and c["url"].startswith("http"):
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
