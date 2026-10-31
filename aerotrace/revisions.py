"""Revision logic: group notices that share a docket, order them by date, and find supersession links."""
import re
from collections import defaultdict
from datetime import date

TODAY = date(2026, 10, 31)  # hackathon date; change to date.today() for real use
_SUPERSEDE_RE = re.compile(r"supersed\w*\s+(?:Airworthiness Directive\s+)?(?:\(AD\)\s+)?(?:AD\s+)?(?:No\.\s*)?(\d{2,4}-\d{2}-\d{2})", re.I)


class RevisionIndex:
    def __init__(self, docs: list[dict], passages: list[dict]):
        self.docs = {d["doc_id"]: d for d in docs}
        self.groups = defaultdict(list)
        for d in docs:
            self.groups[d["docket_group"]].append(d)
        for g in self.groups.values():
            g.sort(key=lambda d: (d["publish_date"] or date.min, d["row"] or 0))

        text_by_doc = defaultdict(str)
        for p in passages:
            text_by_doc[p["doc_id"]] += " " + p["text"]
        self.supersedes = {doc_id: sorted(set(_SUPERSEDE_RE.findall(t))) for doc_id, t in text_by_doc.items()}
        # AD number -> notices that propose to supersede it
        self.superseded_by_ad = defaultdict(list)
        for doc_id, ads in self.supersedes.items():
            for ad in ads:
                self.superseded_by_ad[ad].append(doc_id)

    def siblings(self, doc_id: str) -> list[dict]:
        return self.groups[self.docs[doc_id]["docket_group"]]

    def status(self, doc_id: str) -> dict:
        doc = self.docs[doc_id]
        sib = self.siblings(doc_id)
        latest = sib[-1]
        later_by_ad = [d for d in self.superseded_by_ad.get(doc["ad_number"], []) if d != doc_id] if doc["ad_number"] else []
        due = doc["comments_due"]
        return {
            "used": f"{doc['docket']} | {doc['revision']} | published {doc['publish_date']}",
            "document_id": doc_id,
            "is_latest": latest["doc_id"] == doc_id and not later_by_ad,
            "superseded_by": (latest["doc_id"] if latest["doc_id"] != doc_id else (later_by_ad[0] if later_by_ad else None)),
            "all_revisions": [
                {"document_id": d["doc_id"], "revision": d["revision"], "published": str(d["publish_date"])} for d in sib
            ],
            "withdrawn": any(d["withdrawn"] for d in sib),
            "proposes_to_supersede_ADs": self.supersedes.get(doc_id, []),
            "comment_period": ("unknown" if not due else f"{'OPEN' if due >= TODAY else 'CLOSED'} (due {due})"),
            "stale_warning": bool(doc["issue_date"] and doc["issue_date"].year < 2020),
            "legal_status": "PROPOSED RULE (NPRM) - not a legally binding instruction",
        }
