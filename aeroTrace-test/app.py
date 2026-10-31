"""AeroTrace evidence-first Streamlit interface. Run: streamlit run app.py"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from pathlib import Path

import streamlit as st

from aerotrace.config import settings
from aerotrace.engine import AeroTrace


st.set_page_config(
    page_title="AeroTrace · FAA proposal evidence",
    page_icon="✈",
    layout="wide",
    initial_sidebar_state="collapsed",
)


@st.cache_resource
def get_engine(code_version: tuple[int, int]) -> AeroTrace:
    return AeroTrace(settings)


def current_engine() -> AeroTrace:
    """Recreate the cached engine when its implementation changes during development."""
    package = Path(__file__).parent / "aerotrace"
    return get_engine((
        (package / "engine.py").stat().st_mtime_ns,
        (package / "revision.py").stat().st_mtime_ns,
    ))


@st.cache_data
def corpus_stats(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as con:
        return {
            "fetched": con.execute(
                "SELECT count(*) FROM notices WHERE fetch_status='fetched'"
            ).fetchone()[0],
            "chunks": con.execute("SELECT count(*) FROM chunks").fetchone()[0],
        }


EXAMPLES = (
    "What inspection does FAA-2026-11814 propose?",
    "Does FAA-2026-11814 apply to an EC135P2?",
    "What modification is proposed for the MBB-BK 117 D-2 fuel system?",
    "Was the Boeing 787 proposal in FAA-2025-0204 withdrawn?",
)

RESULT_LABELS = {
    "APPLIES": ("Applies", "positive"),
    "DOES_NOT_APPLY": ("Does not apply", "negative"),
    "UNKNOWN": ("Needs configuration", "caution"),
    "NOT_EVALUATED": ("Not evaluated", "neutral"),
}


def safe(value: object) -> str:
    return html.escape(str(value or ""))


def set_example(question: str) -> None:
    st.session_state["question"] = question
    st.session_state.pop("result", None)
    st.session_state.pop("last_error", None)


def render_pill(label: str, tone: str = "neutral") -> str:
    return f'<span class="pill pill-{tone}">{safe(label)}</span>'


def source_location(source: dict) -> str:
    page = source.get("page") or source.get("pages") or "page not resolved"
    return f"{source.get('docket_number', 'No docket')} · {source.get('publish_date', '')} · page {page}"


st.markdown(
    """
    <style>
      :root {
        --ink: #12233a; --navy: #0b2748; --blue: #176b87; --paper: #f4f7f9;
        --line: #d7e0e7; --muted: #607286; --amber: #c97a16; --red: #a33a3a; --green: #1f7457;
      }
      .stApp {
        background: linear-gradient(90deg, rgba(23,107,135,.035) 1px, transparent 1px) 0 0 / 42px 42px,
          linear-gradient(rgba(23,107,135,.035) 1px, transparent 1px) 0 0 / 42px 42px, var(--paper);
        color: var(--ink);
      }
      [data-testid="stHeader"] { background: rgba(244,247,249,.9); }
      [data-testid="stMainBlockContainer"] { max-width: 1220px; padding-top: 2.2rem; }
      h1, h2, h3, p, label, button, input, textarea { font-family: "Avenir Next", "Segoe UI", sans-serif; }
      h1, h2, h3 { color: var(--ink); letter-spacing: -.025em; }
      .masthead { display:flex; align-items:flex-start; justify-content:space-between; gap:2rem; margin-bottom:2.4rem; }
      .brand-lockup { display:flex; align-items:center; gap:.85rem; }
      .brand-mark { width:2.75rem; height:2.75rem; display:grid; place-items:center; border:1px solid #8ab7c7;
        color:white; background:var(--navy); border-radius:50%; font-size:1.15rem; box-shadow:0 0 0 5px rgba(23,107,135,.08); }
      .brand-name { font-weight:700; font-size:1.25rem; color:var(--navy); letter-spacing:-.03em; }
      .brand-sub { color:var(--muted); font-size:.83rem; margin-top:.08rem; }
      .corpus-lockup { text-align:right; color:var(--muted); font-size:.82rem; line-height:1.5; }
      .corpus-lockup strong { color:var(--ink); font-size:.98rem; }
      .hero { max-width:850px; margin-bottom:1.5rem; }
      .hero h1 { font-size:clamp(2.3rem, 5vw, 4.4rem); line-height:.98; margin:0 0 1rem; font-weight:650; }
      .hero p { max-width:680px; color:var(--muted); font-size:1.08rem; line-height:1.65; }
      .scope-note { border-left:3px solid var(--amber); padding:.25rem 0 .25rem .9rem; color:#6d5537; font-size:.88rem; margin:1.2rem 0 1.6rem; }
      .search-shell { background:white; border:1px solid var(--line); border-top:4px solid var(--navy); padding:1.4rem 1.5rem .8rem; box-shadow:0 14px 35px rgba(11,39,72,.075); }
      .search-heading { font-weight:650; font-size:1.05rem; margin-bottom:.15rem; }
      .search-help { color:var(--muted); font-size:.86rem; margin-bottom:.7rem; }
      div[data-testid="stTextArea"] textarea {
        border-radius:2px; min-height:102px; font-size:1.04rem; line-height:1.5;
        background:#ffffff !important; color:var(--ink) !important; caret-color:var(--blue);
        border-color:#b8c7d1 !important;
      }
      div[data-testid="stTextArea"] textarea::placeholder { color:#7a8998 !important; opacity:1; }
      div[data-testid="stTextInput"] input {
        border-radius:2px; background:#ffffff !important; color:var(--ink) !important;
      }
      div[data-testid="stFormSubmitButton"] button { background:var(--navy); color:white; border:0; border-radius:2px; min-height:3rem; font-weight:650; }
      div[data-testid="stFormSubmitButton"] button:hover { background:var(--blue); color:white; }
      div[data-testid="stButton"] button { border-radius:999px; border-color:#b7c8d3; color:#29465e; background:rgba(255,255,255,.72); }
      div[data-testid="stButton"] button:hover { border-color:var(--blue); color:var(--blue); }
      .examples-title { color:var(--muted); font-size:.82rem; margin:.85rem 0 .15rem; }
      .pipeline { display:grid; grid-template-columns:repeat(4,1fr); border:1px solid var(--line); background:white; margin:2rem 0; }
      .pipeline-step { padding:.85rem 1rem; border-right:1px solid var(--line); min-height:76px; }
      .pipeline-step:last-child { border-right:0; }
      .pipeline-no { color:var(--blue); font-size:.72rem; font-weight:700; }
      .pipeline-name { color:var(--ink); font-size:.86rem; font-weight:650; margin:.18rem 0; }
      .pipeline-copy { color:var(--muted); font-size:.73rem; line-height:1.35; }
      .result-banner { padding:1.25rem 1.5rem; border-left:5px solid var(--green); background:#edf7f3; margin:.8rem 0 1rem; }
      .result-banner.refused { border-left-color:var(--red); background:#fbf0f0; }
      .result-banner h2 { margin:0 0 .25rem; font-size:1.25rem; }
      .result-banner .answer-copy { margin:.8rem 0; color:var(--ink); font-size:1.08rem; line-height:1.75; max-width:85ch; }
      .result-banner .answer-copy a { color:var(--blue); font-weight:700; white-space:nowrap; }
      .answer-meta { color:var(--muted); font-size:.75rem; }
      .section-kicker { color:var(--blue); font-size:.78rem; font-weight:700; margin-bottom:.3rem; }
      .answer-title { font-size:1.85rem; margin:.15rem 0 1.1rem; }
      .synthesis-panel { background:white; border:1px solid #b8ccd7; border-top:5px solid var(--blue); padding:1.25rem; margin-bottom:1rem; box-shadow:0 8px 24px rgba(11,39,72,.06); }
      .synthesis-panel.refused { border-top-color:var(--red); }
      .synthesis-label { display:inline-block; color:white; background:var(--blue); border-radius:2px; padding:.28rem .52rem; font-size:.73rem; font-weight:700; margin-bottom:.65rem; }
      .synthesis-panel.refused .synthesis-label { background:var(--red); }
      .synthesis-panel h2 { font-size:1.45rem; margin:.05rem 0 .35rem; }
      .synthesis-panel > p { color:var(--muted); font-size:.82rem; margin:0; }
      .synthesized-refusal { color:var(--ink); font-size:1.04rem; line-height:1.7; margin-top:1rem; }
      .claim { background:#f8fbfc; border:1px solid var(--line); border-left:4px solid var(--blue); padding:1.1rem 1.2rem; margin:.85rem 0; }
      .claim-text { color:var(--ink); line-height:1.65; font-size:1rem; }
      .claim-source { color:var(--muted); font-size:.78rem; margin-top:.75rem; }
      .pill { display:inline-block; border:1px solid #b9c7d1; border-radius:999px; padding:.2rem .55rem; font-size:.72rem; font-weight:650; margin:.12rem .18rem .12rem 0; }
      .pill-positive { color:var(--green); background:#edf7f3; border-color:#a6ccbd; }
      .pill-negative { color:var(--red); background:#fbf0f0; border-color:#dfb6b6; }
      .pill-caution { color:#825613; background:#fff5df; border-color:#e5c88d; }
      .pill-neutral { color:#506275; background:#f4f6f8; }
      .fact-panel { background:#eaf2f6; border-top:3px solid var(--blue); padding:1.05rem 1.1rem; margin-bottom:1rem; }
      .fact-panel h3 { font-size:.95rem; margin:0 0 .75rem; }
      .fact-row { display:flex; justify-content:space-between; gap:1rem; border-top:1px solid #c9dbe4; padding:.55rem 0; font-size:.8rem; }
      .fact-row span:first-child { color:var(--muted); }
      .fact-row span:last-child { text-align:right; font-weight:600; color:var(--ink); }
      .source-box { background:#fff; border:1px solid var(--line); padding:1rem 1.1rem; line-height:1.6; font-size:.87rem; }
      .source-box mark { background:#ffdf8a; padding:.06rem .12rem; }
      .answer-paragraph { background:white; padding:1.25rem; font-size:1.08rem; line-height:1.85; }
      .answer-paragraph a { white-space:nowrap; font-weight:700; color:var(--blue); }
      .evidence-entry { scroll-margin-top:5rem; margin:1.5rem 0; }
      .evidence-entry:target { outline:2px solid var(--blue); outline-offset:8px; }
      .source-box { white-space:pre-wrap; }
      .empty-state { text-align:center; padding:3.2rem 1rem 2rem; color:var(--muted); }
      .empty-state strong { color:var(--ink); display:block; font-size:1.1rem; margin-bottom:.3rem; }
      .footer-note { margin:2.5rem 0 1rem; padding-top:1rem; border-top:1px solid var(--line); color:var(--muted); font-size:.78rem; }
      @media (max-width: 760px) {
        [data-testid="stMainBlockContainer"] { padding-left:1rem; padding-right:1rem; }
        .masthead { display:block; } .corpus-lockup { text-align:left; margin-top:1rem; } .hero h1 { font-size:2.5rem; }
        .pipeline { grid-template-columns:1fr 1fr; } .pipeline-step:nth-child(2) { border-right:0; }
        .pipeline-step:nth-child(-n+2) { border-bottom:1px solid var(--line); }
      }
      @media (prefers-reduced-motion: reduce) { * { scroll-behavior:auto !important; transition:none !important; } }
    </style>
    """,
    unsafe_allow_html=True,
)


if not settings.database.exists() or not settings.vectors.exists():
    st.error("The evidence index is not ready. Build it with `python scripts/build_index.py`.")
    st.stop()

stats = corpus_stats(settings.database)
st.markdown(
    f"""
    <div class="masthead">
      <div class="brand-lockup"><div class="brand-mark">✈</div><div><div class="brand-name">AeroTrace</div><div class="brand-sub">FAA proposal evidence desk</div></div></div>
      <div class="corpus-lockup"><strong>{stats['fetched']} notices indexed</strong><br>{stats['chunks']:,} citable passages · supplied corpus</div>
    </div>
    <div class="hero"><h1>Ask the proposal.<br>Inspect the proof.</h1>
      <p>Find what an FAA proposed Airworthiness Directive says, test whether its stated applicability matches an aircraft, and trace every answer back to the official notice.</p></div>
    <div class="scope-note"><strong>Scope:</strong> proposal content and history in this dataset. “Latest” means latest in the supplied corpus—not the AD currently in force.</div>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="examples-title">Try a benchmark question</div>', unsafe_allow_html=True)
example_columns = st.columns(4)
for index, example in enumerate(EXAMPLES):
    example_columns[index].button(example, key=f"example_{index}", use_container_width=True, on_click=set_example, args=(example,))

st.markdown('<div class="search-shell"><div class="search-heading">Search the evidence</div><div class="search-help">A docket, model, serial number, or part number makes the result more precise.</div>', unsafe_allow_html=True)
with st.form("evidence_search"):
    question = st.text_area("Question", key="question", placeholder="What inspection does FAA-2026-11814 propose?", label_visibility="collapsed")
    with st.expander("Aircraft details (optional)", expanded=False):
        context_columns = st.columns(4)
        model = context_columns[0].text_input("Model", placeholder="EC135P2")
        serial = context_columns[1].text_input("Serial number", placeholder="1200")
        part = context_columns[2].text_input("Part number", placeholder="3G6410A00131")
        docket = context_columns[3].text_input("Docket", placeholder="FAA-2026-11814")
    submitted = st.form_submit_button("Run evidence search", use_container_width=True)
st.markdown("</div>", unsafe_allow_html=True)

if submitted:
    st.session_state.pop("last_error", None)
    st.session_state.pop("selected_record_id", None)
    if not question.strip():
        st.session_state.pop("result", None)
        st.session_state["last_error"] = "Enter a question before running the evidence search."
    elif not os.getenv("ANTHROPIC_API_KEY"):
        st.session_state.pop("result", None)
        st.session_state["last_error"] = "ANTHROPIC_API_KEY is missing. Add it to .env to enable cited synthesis."
    else:
        try:
            with st.spinner("Ranking passages and checking the selected notice…"):
                st.session_state["result"] = current_engine().ask(
                    question, model=model, serial_number=serial, part_number=part, docket=docket,
                    record_id=st.session_state.get("selected_record_id", ""), keyword_only=False, use_llm=True,
                )
        except Exception as exc:
            st.session_state.pop("result", None)
            st.session_state["last_error"] = f"Search could not run ({type(exc).__name__}). Check the local index and provider connection."

if st.session_state.get("last_error"):
    st.error(st.session_state["last_error"])

result = st.session_state.get("result")
if not result:
    st.markdown('<div class="empty-state"><strong>No search run yet</strong>Ask about a proposal, applicability, revision, inspection, or modification.</div>', unsafe_allow_html=True)
    st.markdown('<div class="footer-note">AeroTrace is a search and verification aid. It does not determine which final AD currently governs an aircraft.</div>', unsafe_allow_html=True)
    st.stop()

answered = result.get("status") == "ANSWERED"
reason_code = result.get("reason_code") or "VERIFIED"
message = result.get("message") or result.get("reason") or "The selected passages support a cited answer."
notice = result.get("notice") or {}
applicability = result.get("applicability") or {}
revision_chain = result.get("revision_chain") or []
parsed = {key: value for key, value in (result.get("parsed") or {}).items() if value and key != "record_id"}
statements = result.get("statements") or []
revision = result.get("revision") or {}

banner_class = "result-banner" if answered else "result-banner refused"
banner_title = "Verified answer" if answered else "Answer withheld"
if statements:
    paragraph = " ".join(
        f'{safe(statement.get("text"))} <a href="#evidence-{index}" target="_self" '
        f'aria-label="View supporting evidence {index}">[{index}]</a>'
        for index, statement in enumerate(statements, start=1)
    )
else:
    paragraph = safe(message)
st.markdown(
    f'<section id="synthesized-answer" class="{banner_class}">'
    f'<h2>{banner_title}</h2><p class="answer-copy">{paragraph}</p>'
    f'<div class="answer-meta">Claude synthesis · {safe(result.get("latency_ms"))} ms total</div></section>',
    unsafe_allow_html=True,
)

if revision:
    count = revision["version_count"]
    selected_date = revision.get("selected_date") or "date unavailable"
    latest_date = revision.get("latest_date") or "date unavailable"
    if count > 1:
        st.info(
            f"{count} versions found. Answer uses version {revision['selected_version']} "
            f"({revision['selected_record']}), published {selected_date}. "
            f"Latest in this corpus: version {revision['latest_version']} "
            f"({revision['latest_record']}), published {latest_date}."
        )
    else:
        st.info(f"1 version found in this corpus: {revision['selected_record']}, published {selected_date}.")
    if not revision["is_latest"] and revision.get("latest_record"):
        if st.button("Answer using latest version", key="use_latest"):
            st.session_state["selected_record_id"] = revision["latest_record"]
            st.session_state["result"] = current_engine().ask(
                result["question"], model=model, serial_number=serial, part_number=part, docket=docket,
                record_id=revision["latest_record"], use_llm=True,
            )
            st.rerun()
    if count > 1:
        with st.expander("Version history", expanded=True):
            for item in revision_chain:
                marker = " · latest" if item["record_id"] == revision.get("latest_record") else ""
                marker += " · selected" if item["record_id"] == revision.get("selected_record") else ""
                st.write(f"Version {item['version']}: {item['record_id']} · {item['publish_date']} · {item.get('official_action') or 'Action unavailable'}{marker}")
                if item.get("source_url"):
                    st.link_button(f"Official notice {item['record_id']}", item["source_url"])
                if item["record_id"] != revision.get("selected_record") and st.button(f"Answer using {item['record_id']}", key=f"version_{item['record_id']}"):
                    st.session_state["selected_record_id"] = item["record_id"]
                    st.session_state["result"] = current_engine().ask(
                        result["question"], model=model, serial_number=serial, part_number=part, docket=docket,
                        record_id=item["record_id"], use_llm=True,
                    )
                    st.rerun()
if notice.get("supersedes_ad"):
    st.caption(f"AD referenced as proposed to be superseded: {notice['supersedes_ad']}")

answer_column, trace_column = st.columns([1.65, 0.85], gap="large")
with answer_column:
    st.markdown("#### Source and applicability")
    st.caption("The answer above links to its highlighted passages in the evidence appendix.")

with trace_column:
    applicability_label, applicability_tone = RESULT_LABELS.get(
        applicability.get("result"), (applicability.get("result") or "Not checked", "neutral")
    )
    latest_record = next((item.get("record_id") for item in revision_chain if item.get("corpus_status") == "latest"), None)
    version_label = ("Latest in corpus" if notice and notice.get("record_id") == latest_record else "Earlier corpus record" if latest_record else "Only corpus record")
    st.markdown(
        f"""
        <div class="fact-panel"><h3>Decision record</h3>
          <div>{render_pill(reason_code, 'positive' if answered else 'negative')} {render_pill(applicability_label, applicability_tone)}</div>
          <div class="fact-row"><span>Docket</span><span>{safe(notice.get('docket_number') or parsed.get('docket') or '—')}</span></div>
          <div class="fact-row"><span>Action</span><span>{safe(notice.get('official_action') or '—')}</span></div>
          <div class="fact-row"><span>Publication</span><span>{safe(notice.get('publish_date') or '—')}</span></div>
          <div class="fact-row"><span>Version</span><span>{safe(version_label)}</span></div>
          <div class="fact-row"><span>Retrieval</span><span>{safe(result.get('search_mode', 'hybrid'))} · {safe(result.get('retrieval_ms'))} ms</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if applicability.get("reason"):
        st.markdown("#### Applicability check")
        st.write(applicability["reason"])
        for check in applicability.get("checks", []):
            tone = "positive" if check.get("result") == "passed" else "negative"
            st.markdown(f"{render_pill(check.get('result', 'checked'), tone)} `{check.get('field')}={check.get('value')}`", unsafe_allow_html=True)

    with st.expander("Audit trace"):
        st.markdown("**1 · Parsed request**")
        st.json(parsed or {"explicit_identifiers": "none"})
        st.markdown("**2 · Ranked documents**")
        st.json((result.get("search") or {}).get("documents", [])[:5])
        st.markdown("**3 · Checked rules**")
        st.json(applicability or {"result": "not evaluated"})
        st.markdown("**4 · Cited source IDs**")
        st.json([statement.get("passage_id") for statement in statements])

    if revision_chain:
        with st.expander("Corpus notice history"):
            st.dataframe(revision_chain, use_container_width=True, hide_index=True)
            st.caption("This is notice history in the supplied corpus, not current legal status.")

    with st.expander("Raw audit JSON"):
        st.download_button("Download audit record", json.dumps(result, indent=2, default=str), file_name="aerotrace-audit.json", mime="application/json", use_container_width=True)

if statements:
    st.divider()
    st.subheader("Evidence appendix")
    st.caption("Highlighted text supports the corresponding reference in the answer.")
    for index, statement in enumerate(statements, start=1):
        source = statement.get("source") or {}
        raw_text = str(source.get("text") or "")
        quote = str(statement.get("evidence_quote") or "").strip()
        match = re.search(re.escape(quote), raw_text, re.IGNORECASE) if quote else None
        highlighted = safe(raw_text)
        if match:
            highlighted = (safe(raw_text[:match.start()]) + "<mark>" +
                           safe(raw_text[match.start():match.end()]) + "</mark>" + safe(raw_text[match.end():]))
        st.markdown(
            f'<section id="evidence-{index}" class="evidence-entry">'
            f'<h3>[{index}] {safe(source.get("subject") or "Official notice passage")}</h3>'
            f'<p>Record {safe(source.get("record_id") or notice.get("record_id") or "unavailable")} · '
            f'{safe(source_location(source))}<br>Federal Register citation: {safe(source.get("citation") or notice.get("citation") or "unavailable")}<br>'
            f'Section: {safe(source.get("section") or "unavailable")} · '
            f'Passage {safe(statement.get("passage_id"))}</p>'
            f'<div class="source-box">{highlighted}</div>'
            '<a href="#synthesized-answer" target="_self">Back to answer</a></section>',
            unsafe_allow_html=True,
        )
        if source.get("source_url"):
            st.link_button(f"Open official notice [{index}]", source["source_url"])

evidence = result.get("evidence") or []
if evidence:
    with st.expander("Additional retrieved evidence"):
        for item in evidence:
            st.markdown(f"**{item.get('section') or 'Unlabelled section'}** · `{item.get('chunk_id')}`")
            st.write(item.get("text"))

st.markdown('<div class="footer-note">Proposal content only. AeroTrace refuses when evidence or aircraft configuration is insufficient and never presents a proposal as an effective requirement.</div>', unsafe_allow_html=True)
