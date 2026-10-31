"""The AeroTrace pipeline: query -> configuration -> applicability filter -> retrieval -> answer -> verification -> JSON.

The LLM (optional) only writes sentences. Every decision about facts, applicability, revisions
and refusals is made by deterministic code in this file and in rules.py / revisions.py / verify.py.
"""
import re
import time
from pathlib import Path

from . import compose, rules, verify
from .config import claude_config
from .index import HybridIndex
from .ingest import REV_CORRECTION, REV_EXTENSION, REV_WITHDRAWAL, load_pdfs, load_tsv
from .revisions import RevisionIndex
from .text import tokenize

MIN_EVIDENCE_SCORE = 6.0  # BM25 score below which we say "no evidence" (calibrated on eval/queries.jsonl)
MIN_COVERAGE = 0.5  # share of the question's topic words that the verified answer must contain
AMBIGUITY_RATIO = 0.9  # 2nd-best directive this close to the best (and no model/docket given) -> ambiguous
SEMANTIC_AMBIGUITY_MARGIN = 0.03  # Conservative heuristic, not a calibrated confidence threshold.
CONFLICT_RATIO = 0.5  # two proposal versions both match at least this well -> conflict

DISCLAIMER = ("Search and verification aid only. Source is a PROPOSED airworthiness directive (NPRM), which is not "
              "legally binding. Always confirm against the official Federal Register / FAA document before acting.")

# Abbreviations that mean different things on different aircraft. Asking with them alone is refused.
AMBIGUOUS_TERMS = {
    "mr": ["main rotor", "maintenance review"],
    "tr": ["tail rotor", "thrust reverser"],
    "ecu": ["engine control unit", "environmental control unit"],
    "pcu": ["power control unit", "propeller control unit"],
}
KNOWN_MAKERS = ["cessna", "piper", "cirrus", "beechcraft", "diamond", "boeing", "airbus", "embraer", "bombardier",
                "comac", "tupolev", "sukhoi", "antonov", "mooney", "pilatus", "daher", "atr", "honda", "tecnam",
                "gulfstream", "dassault", "bell", "sikorsky", "robinson", "leonardo", "mitsubishi", "textron",
                "lockheed", "fokker", "saab", "learjet", "hawker", "socata", "grob", "schweizer", "enstrom", "kaman"]

_DOCKET_RE = re.compile(r"\b(FAA-\d{4}-\d{3,5}(?:R\d|_S|-W)?|\d{2,4}-[A-Z]{2}-\d{1,3}-AD)\b", re.I)
_AD_RE = re.compile(r"\bAD\s+(?:No\.\s*)?(\d{4}-\d{2}-\d{2})\b", re.I)
_RECORD_RE = re.compile(r"\bNPRM-\d+\b", re.I)
_SERIAL_RE = re.compile(r"(?:serial(?:\s+(?:number|no\.?))?|S/N|SN|MSN)\s*[:#]?\s*([A-Z]{0,3}-?\d[\w/-]*)|\b(S-\d{3,})\b", re.I)
_PART_RE = re.compile(r"(?:\bP/N|\bpart(?:\s+number)?)\b\s*[:#]?\s*([A-Z0-9-]*\d[\w-]{2,})", re.I)
_MODELISH_RE = re.compile(r"\b[A-Za-z]{0,6}[-.]?\d[\w.+/-]*\b")
_APPLICABILITY_INTENT = re.compile(
    r"\b(appl(?:y|ies|icable|icability)|affect(?:s|ed)?|my (?:aircraft|airplane|plane|helicopter|engine)|tail number|"
    r"serial|s/n|msn)\b", re.I)
_LATEST_INTENT = re.compile(r"\b(latest|current|newest|most recent)\b", re.I)


class AeroTrace:
    def __init__(self, tsv_path="data/ad_nprm.tsv", pdf_dir="data/pdfs", semantic_index=None):
        docs, passages = load_tsv(tsv_path)
        pdocs, ppassages = load_pdfs(pdf_dir)
        self.docs = {d["doc_id"]: d for d in docs + pdocs}
        self.passages = passages + ppassages
        self.semantic_index = semantic_index
        if semantic_index is not None:
            for chunk in semantic_index.chunks:
                if chunk["doc_id"] not in self.docs:
                    raise ValueError("The semantic index contains documents outside the current dataset.")
                self.passages.append({
                    "passage_id": chunk["chunk_id"], "doc_id": chunk["doc_id"],
                    "paragraph": chunk["paragraph"], "section": chunk["section"], "page": chunk["page"],
                    "text": chunk["text"], "low_confidence": False, "source_url": chunk.get("source_url"),
                    "page_kind": chunk.get("page_kind"), "source_element_id": chunk.get("source_element_id"),
                    "evidence_kind": chunk["evidence_kind"],
                })
        self.pidx = {p["passage_id"]: i for i, p in enumerate(self.passages)}
        self.by_doc = {}
        for i, p in enumerate(self.passages):
            self.by_doc.setdefault(p["doc_id"], []).append(i)
        self.rules = {d: rules.extract_rule(self.docs[d], [self.passages[i] for i in self.by_doc.get(d, [])])
                      for d in self.docs}
        self.revisions = RevisionIndex(list(self.docs.values()), self.passages)
        self.index = HybridIndex(self.passages, self.docs, enable_vectors=False if semantic_index is not None else None)
        self.model_vocab = sorted({m for r in self.rules.values() for m in r["applies_to_models"]}, key=len, reverse=True)
        self.corpus_text = " ".join(
            [p["text"] for p in self.passages] + [d["title"] + " " + d["make_raw"] for d in self.docs.values()]).lower()

    # ---------- query understanding ----------
    def parse_config(self, query: str) -> dict:
        cfg = {"model": None, "serial_number": None, "part_number": None}
        masked = query
        if m := _SERIAL_RE.search(query):
            cfg["serial_number"] = m.group(1) or m.group(2)
            masked = masked.replace(m.group(0), " ")
        if m := _PART_RE.search(masked):
            cfg["part_number"] = m.group(1)
            masked = masked.replace(m.group(0), " ")
        masked = _RECORD_RE.sub(" ", _DOCKET_RE.sub(" ", _AD_RE.sub(" ", masked)))
        # 1) multi-word designations that exactly match a known model ("AS 365 N3", "Falcon 7X")
        words = re.findall(r"[\w.+/-]+", masked)
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                cand = " ".join(words[i:i + n])
                if re.search(r"\d", cand) and any(rules.model_match(cand, v) == "exact" for v in self.model_vocab):
                    cfg["model"] = cand
                    return cfg
        # 2) single tokens; the most specific match wins
        best = None
        for tok in sorted(_MODELISH_RE.findall(masked), key=len, reverse=True):
            kinds = {rules.model_match(tok, v) for v in self.model_vocab}
            if kinds & {"exact", "covered"}:
                cfg["model"] = tok
                return cfg
            if "partial" in kinds and best is None:
                best = tok
        cfg["model"] = best
        return cfg

    def _unknown_terms(self, query: str, cfg: dict) -> list[str]:
        """Makers or model designations the user mentioned that never appear in the corpus."""
        q = query.lower()
        out = [mk for mk in KNOWN_MAKERS if re.search(rf"\b{mk}\b", q) and not re.search(rf"\b{mk}\b", self.corpus_text)]
        masked = _RECORD_RE.sub(" ", _DOCKET_RE.sub(" ", _AD_RE.sub(" ", _SERIAL_RE.sub(" ", _PART_RE.sub(" ", query)))))
        for tok in _MODELISH_RE.findall(masked):
            if not re.search(r"[A-Za-z]", tok) and len(tok) <= 2:
                continue
            if re.fullmatch(r"(19|20)\d\d", tok) or re.fullmatch(r"\d+(st|nd|rd|th|s)?", tok.lower()):
                continue
            if tok.lower() not in self.corpus_text and not any(rules.model_match(tok, v) != "none" for v in self.model_vocab):
                out.append(tok)
        if cfg.get("model") and not any(rules.model_match(cfg["model"], v) != "none" for v in self.model_vocab):
            out.append(cfg["model"])
        return out

    # ---------- main entry ----------
    def ask(self, query: str, model: str | None = None, serial_number: str | None = None,
            part_number: str | None = None, mode: str = "extractive", top_n: int = 20, top_m: int = 6) -> dict:
        t0 = time.perf_counter()
        cfg = self.parse_config(query)
        for k, v in {"model": model, "serial_number": serial_number, "part_number": part_number}.items():
            if v:
                cfg[k] = v.strip()
        res = {"query": query, "status": None, "reason_code": None, "message": None, "configuration": cfg,
               "applicability": None, "statements": [], "removed_statements": [], "revision_status": None,
               "candidates": [], "warnings": [], "mode": mode, "disclaimer": DISCLAIMER, "latency_ms": None,
               "retrieval_mode": "semantic" if self.semantic_index is not None else "bm25", "retrieval": None,
               "llm_called": False, "llm_model": None, "supplemental_evidence": []}
        res["coverage_check"] = None

        def finish(status, reason=None, message=None):
            res.update(status=status, reason_code=reason, message=message,
                       latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            return res

        def refuse(reason, message):
            return finish("REFUSED", reason, message)

        # 1. Ambiguous abbreviations
        for term, meanings in AMBIGUOUS_TERMS.items():
            if re.search(rf"\b{term}\b", query, re.I) and not any(m in query.lower() for m in meanings):
                return refuse("AMBIGUOUS_TERM", f"'{term.upper()}' can mean {' or '.join(meanings)}. "
                                                f"Please spell out which one you mean.")

        # 2. Explicit document / AD references
        allowed = None
        target_docs = set()
        for record_id in _RECORD_RE.findall(query):
            if record_id.upper() not in self.docs:
                return refuse("OUT_OF_SCOPE", f"Record {record_id} is not in the corpus.")
            target_docs.add(record_id.upper())
        for d in _DOCKET_RE.findall(query):
            hits = {i for i, doc in self.docs.items() if doc["docket"].upper() == d.upper()
                    or doc["docket_group"] == d.upper()}
            if not hits:
                return refuse("OUT_OF_SCOPE", f"Docket {d} is not in the corpus. Check the docket number or add the document.")
            target_docs |= hits
        for ad in _AD_RE.findall(query):
            hits = {i for i, doc in self.docs.items() if doc["ad_number"] == ad} | set(self.revisions.superseded_by_ad.get(ad, []))
            hits |= {d for d, idxs in self.by_doc.items() if any(ad in self.passages[i]["text"] for i in idxs)}
            if not hits:
                return refuse("OUT_OF_SCOPE", f"AD {ad} is not mentioned anywhere in the corpus.")
            target_docs |= hits

        # 3. Aircraft or makers that are not in the corpus
        unknown = self._unknown_terms(query, cfg)
        if unknown and not target_docs:
            return refuse("OUT_OF_SCOPE", f"No documents in the corpus cover: {', '.join(unknown)}. "
                                          f"The corpus contains FAA proposed ADs only.")

        # 4. Applicability filter: keep only documents whose rule could cover the user's model
        if cfg["model"] and not target_docs:
            target_docs = {d for d, r in self.rules.items()
                           if any(rules.model_match(cfg["model"], m) != "none" for m in r["applies_to_models"])}
            if not target_docs:
                return refuse("OUT_OF_SCOPE", f"No document in the corpus lists model {cfg['model']}.")
        if target_docs:
            allowed = {i for d in target_docs for i in self.by_doc.get(d, [])}

        strict = bool(_APPLICABILITY_INTENT.search(query)) or bool(cfg["serial_number"] or cfg["part_number"])
        if strict and not cfg["model"] and len(target_docs) != 1:
            return refuse("MISSING_CONFIG", "Applicability depends on the aircraft. Please provide the aircraft model "
                                            "(and serial number if known).")

        # 5. Retrieval
        if self.semantic_index is not None:
            retrieved = self.semantic_index.search(query, top_n, top_m, allowed_doc_ids=target_docs or None)
            res["retrieval"] = retrieved
            hits = [(self.pidx[chunk["chunk_id"]], chunk["score"]) for chunk in retrieved["chunks"]]
        else:
            hits = self.index.search(query, allowed, k=30)
        if not hits or (self.semantic_index is None and hits[0][1] < MIN_EVIDENCE_SCORE and not target_docs):
            return refuse("NO_EVIDENCE", "No passage in the corpus supports an answer. Try naming the aircraft model, "
                                         "the part or system, or the docket number.")
        doc_scores = {}
        for i, s in hits:
            d = self.passages[i]["doc_id"]
            doc_scores[d] = max(doc_scores.get(d, 0), s)
        ranked = sorted(doc_scores, key=lambda d: -doc_scores[d])
        res["candidates"] = [{"document_id": d, "title": self.docs[d]["title"], "score": round(doc_scores[d], 2),
                              "models": self.rules[d]["applies_to_models"][:8]} for d in ranked[:5]]
        top = ranked[0]

        # 6. Ambiguity between different aircraft
        if not target_docs and len(ranked) > 1:
            second = ranked[1]
            different_group = self.docs[top]["docket_group"] != self.docs[second]["docket_group"]
            close = (doc_scores[top] - doc_scores[second] <= SEMANTIC_AMBIGUITY_MARGIN
                     if self.semantic_index is not None else doc_scores[second] >= AMBIGUITY_RATIO * doc_scores[top])
            if different_group and close:
                return refuse("AMBIGUOUS_TERM", "The question matches several different directives about equally well "
                                                "(see candidates). Please name the aircraft model or docket number.")

        # 7. Revision logic
        #    - withdrawal / comment-extension notices carry no new content: answer from the proposal, flag it
        #    - an OFR correction republishes the proposal: use it automatically
        #    - two different proposal versions that both match: refuse with CONFLICT unless the user asks for "latest"
        content = [d["doc_id"] for d in self.revisions.siblings(top)
                   if not d["revision"].startswith((REV_WITHDRAWAL, REV_EXTENSION))] or [top]
        latest = content[-1]
        matched = [d for d in content if doc_scores.get(d, 0) >= CONFLICT_RATIO * doc_scores[top]]
        if top != latest or len(matched) > 1:
            if self.docs[latest]["revision"].startswith(REV_CORRECTION) or _LATEST_INTENT.search(query):
                top = latest
            else:
                versions = list(dict.fromkeys(matched + [latest]))
                evs = {d: rules.evaluate(self.rules[d], cfg)["result"] for d in versions}
                decisive = set(evs.values()) <= {"APPLIES", "DOES_NOT_APPLY"}
                if strict and decisive and len(set(evs.values())) == 1:
                    top = latest  # every version gives the same applicability answer: no real conflict
                elif (strict and decisive) or len(matched) > 1:
                    res["revision_status"] = self.revisions.status(top)
                    res["candidates"] = [{"document_id": d, "title": self.docs[d]["title"], "revision": self.docs[d]["revision"],
                                          "published": str(self.docs[d]["publish_date"]), "applicability": evs[d]}
                                         for d in versions]
                    return refuse("CONFLICT", f"{len(versions)} versions of the proposal in docket "
                                              f"{self.docs[top]['docket_group']} match and differ: {', '.join(versions)}. "
                                              f"Ask for the 'latest' version or name one explicitly.")
                # else: only an older version matches; answer from it with a not-latest warning
        res["revision_status"] = rev = self.revisions.status(top)
        if not rev["is_latest"]:
            res["warnings"].append(f"This is NOT the latest revision. Newer: {rev['superseded_by']}.")
        if rev["withdrawn"]:
            res["warnings"].append("This proposal (or a revision of it) has been WITHDRAWN.")
        if rev["stale_warning"]:
            res["warnings"].append("Issued before 2020: may be superseded by later rulemaking outside this corpus.")
        res["warnings"].append(f"Comment period: {rev['comment_period']}.")

        # 8. Applicability evaluation (deterministic)
        rule = self.rules[top]
        if strict or cfg["model"]:
            ev = rules.evaluate(rule, cfg)
            res["applicability"] = ev
            if not strict and ev["result"] in ("UNKNOWN", "UNPARSED"):
                # The user named a model only to narrow the search; report, but don't block the answer.
                res["warnings"].append(f"Applicability to your exact aircraft is not confirmed ({ev['result']}). "
                                       f"Ask 'does this apply to <model> serial <number>?' to check.")
            elif ev["result"] == "UNKNOWN":
                return refuse("MISSING_CONFIG", f"To decide applicability of {top}, please provide: "
                                                f"{', '.join(ev['missing_fields'])}."
                                                + (f" ({ev['failed_condition']})" if ev["failed_condition"] else ""))
            elif ev["result"] == "UNPARSED":
                return refuse("UNPARSED_RULE", f"The applicability rule of {top} could not be parsed reliably: "
                                               f"{ev['failed_condition']} See {rule['source_ref']}.")
        else:
            res["applicability"] = {"result": "NOT_EVALUATED", "reason": "No aircraft configuration in the question.",
                                    "rule_ref": rule["source_ref"]}

        # 9. Compose from ONE document only (never blend directives)
        d = self.docs[top]
        ignore = set(tokenize(" ".join([d["make"], d["make_raw"], d["docket"], d["doc_id"], cfg["model"] or "",
                                        cfg["serial_number"] or "", cfg["part_number"] or ""] + rule["applies_to_models"])))
        cand = [self.passages[i] for i, _ in hits if self.passages[i]["doc_id"] == top]
        if self.semantic_index is None:
            cand = cand[:5]
        elif not cand:
            return refuse("NO_EVIDENCE", "The selected revision has no passages in the retrieved top M chunks. "
                                         "Increase M or name the revision explicitly.")
        summary_p = next((self.passages[i] for i in self.by_doc.get(top, []) if self.passages[i]["paragraph"] == "summary"), None)
        if summary_p and summary_p not in cand and self.semantic_index is None:
            cand.append(summary_p)
        rule_p = self.passages[self.pidx[rule["source_ref"]]]
        rule_sentence = compose.extractive("applies model serial numbers " + " ".join(rule["applies_to_models"][:3]),
                                           [rule_p], self.index.bm25.idf, max_sentences=1)
        if res["applicability"]["result"] in ("DOES_NOT_APPLY", "APPLIES") and strict:
            if rule_p not in cand:
                res["supplemental_evidence"].append({"passage_id": rule_p["passage_id"], "reason": "applicability rule"})
            cand = [rule_p] + [p for p in cand if p is not rule_p]
        if res["applicability"]["result"] == "DOES_NOT_APPLY":
            drafted = rule_sentence
            res["mode"] = "extractive"
        elif mode == "llm":
            res["llm_called"] = True
            res["llm_model"] = claude_config().model
            drafted, err = compose.llm(query, cand, context={
                "configuration": cfg, "applicability": res["applicability"], "revision_status": rev,
            })
            if err:
                res["warnings"].append(f"LLM unavailable ({err}); used extractive mode.")
                res["mode"] = "extractive"
                drafted = compose.extractive(query, cand, self.index.bm25.idf, ignore=ignore)
        else:
            drafted = compose.extractive(query, cand, self.index.bm25.idf, fallback_summary=bool(target_docs), ignore=ignore)
        if strict and res["applicability"]["result"] == "APPLIES":
            # Lead an applicability answer with the rule text that proves it.
            drafted = rule_sentence + [s for s in drafted if s["text"] not in {r["text"] for r in rule_sentence}]

        # 10. Verification: every sentence must be supported by the passage it cites
        allowed_ids = {p["passage_id"]: p for p in cand}
        for st in drafted:
            p = allowed_ids.get(st["passage_id"])
            chk = verify.check(st["text"], p["text"]) if p else {"verified": False, "overlap": 0, "reason": "cites a passage that was not retrieved"}
            if not chk["verified"]:
                res["removed_statements"].append({**st, **chk})
                continue
            d = self.docs[p["doc_id"]]
            res["statements"].append({
                "text": st["text"],
                "verification": chk,
                "citations": [{"document_id": d["doc_id"], "docket": d["docket"], "revision": d["revision"],
                               "page": p["page"], "paragraph": p["paragraph"], "section": p["section"],
                               "fr_citation": d["fr_citation"], "url": p.get("source_url") or d["url"], "source_text": p["text"],
                               "passage_id": p["passage_id"], "page_kind": p.get("page_kind"), "page_range": d["pages"],
                               "evidence_kind": p.get("evidence_kind"), "source_element_id": p.get("source_element_id")}],
            })
        if not res["statements"]:
            if drafted:
                return refuse("VERIFICATION_FAILED", "Generated sentences could not be traced to the source text, so "
                                                     "no answer is shown. Try rephrasing or open the source document.")
            return refuse("NO_EVIDENCE", f"Document {top} was found but no passage answers this question.")
        if res["applicability"]["result"] == "DOES_NOT_APPLY":
            return finish("ANSWERED", message=f"Does NOT apply: {res['applicability']['failed_condition']}")
        if not strict:
            # The answer must actually talk about what was asked (e.g. "torque", "flap track"), not just the aircraft.
            cov, missing = compose.coverage(query, res["statements"], self.index.bm25.idf, ignore)
            res["coverage_check"] = {"score": round(cov, 4), "minimum": MIN_COVERAGE, "missing_terms": missing}
            if cov < MIN_COVERAGE:
                res["statements"] = []
                return refuse("NO_EVIDENCE", f"The source-checked answer from {top} did not sufficiently address "
                                             f"these requested topics: {', '.join(missing)}. "
                                             "The answer was withheld because the topic check failed.")
        return finish("ANSWERED")


_ENGINE = None


def get_engine() -> AeroTrace:
    global _ENGINE
    if _ENGINE is None:
        root = Path(__file__).resolve().parent.parent
        _ENGINE = AeroTrace(root / "data" / "ad_nprm.tsv", root / "data" / "pdfs")
    return _ENGINE
