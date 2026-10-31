from __future__ import annotations

import re
import threading
import time

from .config import Settings, settings as default_settings
from .embed import VectorIndex
from .query import parse_query
from .retrieve import evidence_for_document, hybrid_search
from .revision import resolve
from .rules import evaluate_applicability
from .schema import connect
from .synthesize import synthesize, synthesize_refusal


class AeroTrace:
    def __init__(self, settings: Settings = default_settings):
        self.settings = settings
        self.vectors = VectorIndex(settings)
        self._lock = threading.RLock()

    def close(self) -> None:
        """Retained for callers; database connections are scoped to each request."""

    def _refuse(self, question: str, base: dict, started: float, *, reason_code: str,
                verified_reason: str, context: dict, use_llm: bool, extra: dict | None = None) -> dict:
        """Return an enforced refusal, using Claude only to phrase the final explanation."""
        answer = {"status": "REFUSED", "reason": verified_reason, "statements": [],
                  "response_origin": "deterministic"}
        if use_llm:
            llm_started = time.perf_counter()
            try:
                answer = synthesize_refusal(
                    self.settings,
                    question,
                    reason_code=reason_code,
                    verified_reason=verified_reason,
                    context=context,
                )
                answer["response_origin"] = "anthropic"
            except Exception as exc:
                answer["synthesis_error"] = type(exc).__name__
                answer["response_origin"] = "deterministic_fallback"
            answer["llm_ms"] = round((time.perf_counter() - llm_started) * 1000, 1)
        return {
            **base,
            **(extra or {}),
            **answer,
            "reason_code": reason_code,
            "message": answer["reason"],
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def ask(self, question: str, *, model: str = "", serial_number: str = "", part_number: str = "",
            docket: str = "", record_id: str = "", keyword_only: bool = False, use_llm: bool = True) -> dict:
        with self._lock:
            con = connect(self.settings.database)
            try:
                return self._ask(con, question, model=model, serial_number=serial_number,
                                 part_number=part_number, docket=docket, record_id=record_id,
                                 keyword_only=keyword_only, use_llm=use_llm)
            finally:
                con.close()

    def _ask(self, con, question: str, *, model: str = "", serial_number: str = "", part_number: str = "",
             docket: str = "", record_id: str = "", keyword_only: bool = False, use_llm: bool = True) -> dict:
        started = time.perf_counter()
        parsed = parse_query(con, question, {
            "model": model, "serial_number": serial_number, "part_number": part_number, "docket": docket,
        })
        selection = resolve(con, question, parsed, record_id)
        if selection.get("requested_record") and not selection.get("error"):
            parsed["record_id"] = selection["requested_record"]
        retrieval_started = time.perf_counter()
        if selection.get("error"):
            base = {"question": question, "parsed": parsed, "retrieval_ms": 0, "search_mode": "hybrid", "search": {"hits": [], "documents": []}}
            return self._refuse(question, base, started, reason_code=selection["error"],
                                verified_reason=selection["reason"], context=parsed, use_llm=use_llm)
        search = hybrid_search(con, self.vectors, question, parsed, keyword_only=keyword_only)
        if not selection.get("docket") and search["documents"]:
            first = con.execute("SELECT docket_number FROM notices WHERE record_id=?", (search["documents"][0],)).fetchone()
            selection = resolve(con, question, {**parsed, "docket": first["docket_number"]}, record_id)
            if selection.get("error"):
                base = {"question": question, "parsed": parsed, "search_mode": "hybrid", "search": search, "retrieval_ms": round((time.perf_counter()-retrieval_started)*1000, 1)}
                return self._refuse(question, base, started, reason_code=selection["error"],
                                    verified_reason=selection["reason"], context=parsed, use_llm=use_llm)
            parsed["record_id"] = selection["requested_record"]
            if parsed["record_id"] != search["documents"][0]:
                search = hybrid_search(con, self.vectors, question, parsed, keyword_only=keyword_only)
        revision_info = selection.get("revision")
        revision = selection.get("history", [])
        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000, 1)
        base = {"question": question, "parsed": parsed, "retrieval_ms": retrieval_ms,
                "search_mode": "keyword" if keyword_only else "hybrid", "search": search}
        if not search["documents"]:
            reason_code = search["reason"] or "NO_EVIDENCE"
            return self._refuse(
                question, base, started, reason_code=reason_code,
                verified_reason="No official source passage in the indexed corpus supports this question.",
                context={"parsed_request": parsed, "retrieval_reason": reason_code}, use_llm=use_llm,
            )
        explicit_scope = any(parsed.get(key) for key in ("docket", "ad_number", "model"))
        semantic_max = search.get("semantic_max")
        if not keyword_only and not explicit_scope and semantic_max is not None and semantic_max < 0.30:
            return self._refuse(
                question, base, started, reason_code="OUT_OF_SCOPE",
                verified_reason="The question is not sufficiently related to any official notice in this corpus.",
                context={"parsed_request": parsed, "semantic_max": semantic_max}, use_llm=use_llm,
            )
        if not parsed.get("docket") and len(search["documents"]) > 1:
            top_scores = []
            for record_id in search["documents"][:2]:
                score = max(h["rrf_score"] for h in search["hits"] if h["record_id"] == record_id)
                top_scores.append(score)
            if top_scores[1] >= top_scores[0] * 0.97:
                return self._refuse(
                    question, base, started, reason_code="AMBIGUOUS_DOCUMENT",
                    verified_reason="Several notices match almost equally. Provide a docket or aircraft model.",
                    context={"parsed_request": parsed, "candidate_records": search["documents"][:5]},
                    use_llm=use_llm,
                )
        record_id = search["documents"][0]
        notice = dict(con.execute("SELECT * FROM notices WHERE record_id=?", (record_id,)).fetchone())
        if notice["fetch_status"] != "fetched":
            return self._refuse(question, base, started, reason_code="SOURCE_UNAVAILABLE",
                                verified_reason="The selected notice's official full text is unavailable.",
                                context={"revision": revision_info}, use_llm=use_llm,
                                extra={"notice": notice, "revision": revision_info, "revision_chain": revision})
        evidence = evidence_for_document(con, search, record_id)
        app_chunks = [p for p in evidence if "applicability" in str(p.get("section", "")).lower()]
        applicability = evaluate_applicability(notice, app_chunks, parsed)
        if not revision:
            selection = resolve(con, question, {**parsed, "docket": notice["docket_number"]}, record_id)
            revision, revision_info = selection.get("history", []), selection.get("revision")
        checks = {
            "document": {"record_id": record_id, "docket": notice["docket_number"],
                         "action": notice["official_action"], "proposal_only": True},
            "applicability": applicability,
            "revision_scope": "latest in supplied corpus only",
            "selected_revision": revision_info,
        }
        applicability_intent = bool(re.search(r"\b(appl(?:y|ies|icable|icability)|affected)\b", question, re.I))
        if applicability_intent and applicability["result"] in {"UNKNOWN", "NOT_EVALUATED"}:
            verified_reason = applicability.get("reason") or "More aircraft configuration is required."
            return self._refuse(
                question, base, started, reason_code="MISSING_CONFIG", verified_reason=verified_reason,
                context={"parsed_request": parsed, "applicability": applicability,
                         "document": checks["document"]}, use_llm=use_llm,
                extra={"notice": notice, "applicability": applicability,
                       "revision_chain": revision, "revision": revision_info, "evidence": evidence},
            )
        if use_llm:
            llm_started = time.perf_counter()
            try:
                answer = synthesize(self.settings, question, checks, evidence)
                answer["response_origin"] = "anthropic"
            except Exception as exc:
                answer = {"status": "REFUSED", "reason": f"Synthesis unavailable: {type(exc).__name__}",
                          "statements": [], "removed_statements": [],
                          "response_origin": "deterministic_fallback"}
            answer["llm_ms"] = round((time.perf_counter() - llm_started) * 1000, 1)
        else:
            answer = {"status": "ANSWERED", "reason": None, "statements": [
                {"text": p["text"], "passage_id": p["chunk_id"], "evidence_quote": p["text"], "source": p}
                for p in evidence[:2]
            ]}
        return {**base, **answer, "reason_code": None if answer["status"] == "ANSWERED" else "NO_VERIFIED_ANSWER",
                "notice": notice, "applicability": applicability, "revision_chain": revision, "revision": revision_info,
                "evidence": evidence, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}

