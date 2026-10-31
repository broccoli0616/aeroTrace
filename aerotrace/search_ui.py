"""Streamlit view of the two-stage evidence retrieval workflow."""
import json
from pathlib import Path

import streamlit as st

from .embeddings import EmbeddingError
from .config import ROOT, claude_config
from .engine import AeroTrace
from .retrieval import DEFAULT_INDEX, RetrievalError, TwoStageIndex


def _fingerprint() -> tuple:
    manifest_path = DEFAULT_INDEX / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [manifest_path, Path(manifest["prepared_path"])]
    paths.extend(Path(path) for path in manifest.get("source_files", {}))
    return tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)


@st.cache_resource
def _index(fingerprint):
    return TwoStageIndex()


@st.cache_resource
def _answer_engine(fingerprint):
    return AeroTrace(ROOT / "data" / "ad_nprm.tsv", ROOT / "data" / "pdfs", semantic_index=_index(fingerprint))


def _render_answer(answer: dict) -> None:
    st.subheader("Answer")
    if answer["status"] == "ANSWERED":
        st.success(answer["message"] or "Answer with source checks")
    else:
        st.error(f"{answer['reason_code']}: {answer['message']}")
    if answer["llm_called"]:
        st.caption(f"Claude model: {answer['llm_model']} · Answer mode: {answer['mode']} · "
                   f"Total retrieval + answer time: {answer['latency_ms']} ms")
    else:
        st.caption("Claude was not called; the deterministic checks supplied this result.")
    for warning in answer["warnings"]:
        st.warning(warning)
    applicability = answer.get("applicability") or {}
    if applicability.get("result"):
        st.caption(f"Applicability check: {applicability['result']}")
    for number, statement in enumerate(answer["statements"], start=1):
        citation = statement["citations"][0]
        st.markdown(f"**{number}.** {statement['text']}")
        st.caption(f"{citation['document_id']} · {citation['docket']} · {citation['revision']} · "
                   f"source: {citation['paragraph']} · check: {statement['verification']['reason']}")
        with st.expander(f"Source for answer sentence {number}"):
            st.text(citation["source_text"])
            if citation.get("page") is not None:
                st.caption(f"Source page: {citation['page']}")
            elif citation.get("page_range"):
                st.caption(f"Record page range: {citation['page_range']}; exact passage page unavailable.")
            if citation.get("url"):
                st.link_button("Open cited notice", citation["url"])
    if answer["supplemental_evidence"]:
        st.caption("The deterministic applicability check added its original rule passage to the retrieved evidence.")
    if answer["removed_statements"]:
        with st.expander("Sentences removed by source checks"):
            st.json(answer["removed_statements"])
    st.caption(answer["disclaimer"])
    with st.expander("Answer audit JSON"):
        st.json(answer)
        st.download_button("Download answer audit (JSON)", json.dumps(answer, indent=2, default=str),
                           file_name="aerotrace_answer.json", mime="application/json")


def render_search(examples: list[str]) -> None:
    st.caption("Find evidence in two stages: question → top N documents → top M chunks. "
               "Then ask Claude for an answer with applicability, revision, and source checks.")
    try:
        fingerprint = _fingerprint()
        index = _index(fingerprint)
    except (OSError, ValueError, KeyError, RetrievalError) as exc:
        st.info("Build the semantic search index before searching.")
        st.code("python -m aerotrace.prepare\npython -m aerotrace.retrieval build", language="powershell")
        if isinstance(exc, RetrievalError):
            st.error(str(exc))
        return
    manifest = index.manifest
    config = claude_config()
    with st.sidebar:
        st.header("Search size")
        top_n = int(st.number_input("N: candidate documents", min_value=1, max_value=manifest["documents"],
                                    value=min(20, manifest["documents"]), step=1))
        top_m = int(st.number_input("M: evidence chunks", min_value=1, max_value=100, value=6, step=1))
        st.caption(f"Embedding model: {manifest['model']}")
        st.header("Claude answer")
        st.caption(f"Model: {config.model}")
        if not config.api_key:
            st.info("Set ANTHROPIC_API_KEY in .env to enable answers.")
        with st.expander("Aircraft configuration for answers"):
            st.caption("Optional. These override values detected in the question when generating an answer.")
            model = st.text_input("Model", key="semantic_model", placeholder="e.g. R22")
            serial = st.text_input("Serial number", key="semantic_serial", placeholder="e.g. 1200")
            part = st.text_input("Installed part number", key="semantic_part", placeholder="e.g. 3G6410A00131")
        st.header("Try an example")
        for example in examples:
            if st.button(example, key=f"semantic-example-{example}", use_container_width=True):
                st.session_state["semantic_query"] = example
    st.info(f"Indexed corpus: {manifest['documents']} documents and {manifest['chunks']} chunks. "
            f"Evidence types: {manifest['evidence_kinds'].get('document', 0)} document-text chunks, "
            f"{manifest['evidence_kinds'].get('summary', 0)} summary chunks, "
            f"{manifest['evidence_kinds'].get('metadata', 0)} metadata chunks.")
    query = st.text_input("Ask a question", key="semantic_query", placeholder="What could cause R22 teeter stop brackets to fail?")
    signature = (query.strip(), top_n, top_m, fingerprint, model, serial, part)
    answer_signature = (signature, model, serial, part, config.model)
    search_column, answer_column = st.columns(2)
    search_clicked = search_column.button("Search evidence", disabled=not query.strip())
    answer_clicked = answer_column.button("Answer with Claude", type="primary", disabled=not query.strip() or not config.api_key)
    if search_clicked:
        try:
            with st.spinner("Finding candidate documents and matching chunks…"):
                result = index.search(query.strip(), top_n, top_m)
            st.session_state["semantic_result"] = {"signature": signature, "result": result}
            st.session_state.pop("semantic_answer", None)
        except (EmbeddingError, RetrievalError, ValueError) as exc:
            st.error(str(exc))
            return
    if answer_clicked:
        try:
            with st.spinner("Retrieving evidence, checking applicability/revisions, and asking Claude…"):
                answer = _answer_engine(fingerprint).ask(query.strip(), model=model or None,
                            serial_number=serial or None, part_number=part or None, mode="llm", top_n=top_n, top_m=top_m)
            st.session_state["semantic_answer"] = {"signature": answer_signature, "answer": answer}
            if answer["retrieval"] is not None:
                st.session_state["semantic_result"] = {"signature": signature, "result": answer["retrieval"]}
            else:
                st.session_state.pop("semantic_result", None)
        except (EmbeddingError, RetrievalError, ValueError) as exc:
            st.error(str(exc))
            return
    saved_answer = st.session_state.get("semantic_answer")
    if saved_answer and saved_answer["signature"] == answer_signature:
        _render_answer(saved_answer["answer"])
    saved = st.session_state.get("semantic_result")
    if not saved or saved["signature"] != signature:
        if query.strip():
            if not saved_answer or saved_answer["signature"] != answer_signature:
                st.caption("Select Search evidence or Answer with Claude to run this question with the current N and M.")
        return
    result = saved["result"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Candidate documents", len(result["documents"]))
    c2.metric("Retrieved chunks", len(result["chunks"]))
    c3.metric("Total search time", f"{result['latency_ms']} ms")
    st.caption(f"Question embedding/cache: {result['embedding_ms']} ms · Local ranking: {result['ranking_ms']} ms. "
               "Cosine similarity ranks related text; it is not a probability that a claim is correct.")
    if result["reference_filter"]:
        st.caption("Search restricted to the named reference(s): " + ", ".join(result["reference_filter"]))
    documents_tab, chunks_tab, json_tab = st.tabs(["1. Top N documents", "2. Top M chunks", "JSON"])
    with documents_tab:
        st.dataframe([{"Rank": d["rank"], "Record": d["doc_id"], "Docket": d["docket"],
                       "Similarity": d["score"], "Title": d["title"], "Published": d.get("publish_date"),
                       "Source availability": d["source_status"], "Summary basis": d["summary_basis"]}
                      for d in result["documents"]], hide_index=True, use_container_width=True)
    with chunks_tab:
        st.caption("Every chunk below belongs to one of the candidate documents shown in stage 1.")
        for chunk in result["chunks"]:
            label = (f"{chunk['rank']}. {chunk['doc_id']} · {chunk['evidence_kind']} · "
                     f"similarity {chunk['score']:.3f} · {chunk['section'] or chunk['paragraph']}")
            with st.expander(label, expanded=chunk["rank"] <= 2):
                st.caption(f"{chunk['docket']} · {chunk.get('revision') or 'revision unknown'} · "
                           f"document rank {chunk['document_rank']} · source block {chunk['paragraph']}")
                st.text(chunk["text"])
                if chunk["page"] is not None:
                    st.caption(f"Source page: {chunk['page']} ({chunk['page_kind']})")
                elif chunk.get("page_range"):
                    st.caption(f"Record page range: {chunk['page_range']}. Exact chunk page is not available.")
                if chunk.get("source_element_id"):
                    st.caption(f"HTML source element: {chunk['source_element_id']}")
                st.caption(chunk.get("fr_citation") or "")
                if chunk.get("source_url"):
                    st.link_button("Open source notice", chunk["source_url"])
    with json_tab:
        st.json(result)
        st.download_button("Download search evidence (JSON)", json.dumps(result, indent=2),
                           file_name="aerotrace_search.json", mime="application/json")
