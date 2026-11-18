"""
JURIES PROOF - Analysis engine (all deterministic, from real extracted text).

- Contradiction detection across the case's real documents
- Timeline extraction (dates + events, each with document/page source)
- Evidence coverage calculation
- Source quality classification
- Missing-information suggestions
- Case review assembly
"""
import re
import uuid
from datetime import datetime

from .db import query, execute, log_activity
from .retrieval import tokenize

DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*,?\s*\d{0,4}\b"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*\d{0,4})",
    re.IGNORECASE,
)
AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr|usd|\$)\s?[\d,]+(?:\.\d+)?", re.IGNORECASE)


def _now():
    return datetime.utcnow().isoformat()


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


# ------------------------------------------------------------------
# Contradiction detection
# ------------------------------------------------------------------

def detect_contradictions(case_id, store=True):
    """Find sentences in DIFFERENT documents that share topic terms but
    assert different dates/amounts."""
    rows = query(
        "SELECT c.id, c.document_id, c.page_number, c.section, c.text, d.doc_name "
        "FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.case_id = ? "
        "AND d.processing_status = 'ready'",
        (case_id,),
    )
    sents = []
    for r in rows:
        for s in re.split(r"(?<=[.!?])\s+", r["text"]):
            s = _norm(s)
            lines = [l for l in s.split("\n") if l.strip()]
            if lines and len("\n".join(lines)) < len(s) + 2:
                s = " ".join(l.strip() for l in lines if len(l.strip()) > 3)
            if len(s) < 30:
                continue
            dates = DATE_RE.findall(s)
            amounts = AMOUNT_RE.findall(s)
            if dates or amounts:
                sents.append({"doc": r["doc_name"], "doc_id": r["document_id"],
                              "page": r["page_number"], "section": r["section"],
                              "text": s, "dates": dates, "amounts": amounts,
                              "tokens": set(tokenize(s))})

    found, seen = [], set()
    for i in range(len(sents)):
        for j in range(i + 1, len(sents)):
            a, b = sents[i], sents[j]
            if a["doc_id"] == b["doc_id"] and a["page"] == b["page"]:
                continue  # same page of same doc is not a cross-document conflict
            shared = a["tokens"] & b["tokens"]
            # must share meaningful topic vocabulary; require calendar entities on both sides
            if len(shared) < 4:
                continue
            if not (a["dates"] or a["amounts"]) or not (b["dates"] or b["amounts"]):
                continue
            conflict = None
            for da in a["dates"]:
                if not any(_norm(da).lower() == _norm(db).lower() for db in b["dates"]) and b["dates"]:
                    conflict = ("date", da, b["dates"][0])
                    break
            if not conflict:
                for aa in a["amounts"]:
                    if not any(_norm(aa).lower() == _norm(ab).lower() for ab in b["amounts"]) and b["amounts"]:
                        conflict = ("amount", aa, b["amounts"][0])
                        break
            if conflict:
                key = (a["text"][:60], b["text"][:60])
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "topic": ", ".join(sorted(shared)[:6]),
                    "kind": conflict[0],
                    "claim_a": a["text"], "claim_b": b["text"],
                    "doc_a": a["doc"], "page_a": a["page"], "section_a": a["section"],
                    "doc_b": b["doc"], "page_b": b["page"], "section_b": b["section"],
                    "excerpt_a": a["text"], "excerpt_b": b["text"],
                })
    if store:
        execute("DELETE FROM contradictions WHERE case_id = ?", (case_id,))
        for c in found:
            execute(
                "INSERT INTO contradictions (id, case_id, topic, claim_a, claim_b, doc_a, page_a, doc_b, page_b, excerpt_a, excerpt_b, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"CON-{uuid.uuid4().hex[:10]}", case_id, c["topic"], c["claim_a"], c["claim_b"],
                 c["doc_a"], c["page_a"], c["doc_b"], c["page_b"], c["excerpt_a"], c["excerpt_b"],
                 "DETECTED", _now()),
            )
        for c in found:
            log_activity(case_id, "contradiction_detected",
                         f"Contradiction on {c['kind']}: {c['doc_a']} p{c['page_a']} vs {c['doc_b']} p{c['page_b']}")
    return found


# ------------------------------------------------------------------
# Timeline extraction
# ------------------------------------------------------------------

def extract_timeline(case_id, store=True):
    """Extract dated sentences from the real chunks; each event keeps its source."""
    rows = query(
        "SELECT c.document_id, c.page_number, c.section, c.text, d.doc_name "
        "FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE c.case_id = ? AND d.processing_status = 'ready'",
        (case_id,),
    )
    events = []
    seen = set()
    for r in rows:
        for s in re.split(r"(?<=[.!?])\s+", r["text"]):
            s = _norm(s)
            if len(s) < 25 or len(s) > 400:
                continue
            m = DATE_RE.search(s)
            if not m:
                continue
            key = (r["document_id"], r["page_number"], s[:80])
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "event_date": m.group(0),
                "description": s,
                "document_id": r["document_id"],
                "doc_name": r["doc_name"],
                "page_number": r["page_number"],
                "section": r["section"],
            })
    if store:
        execute("DELETE FROM events WHERE case_id = ?", (case_id,))
        for e in events:
            execute(
                "INSERT INTO events (id, case_id, event_date, description, document_id, doc_name, page_number, excerpt, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"EVT-{uuid.uuid4().hex[:10]}", case_id, e["event_date"], e["description"],
                 e["document_id"], e["doc_name"], e["page_number"], e["description"], _now()),
            )
        if events:
            log_activity(case_id, "timeline_built", f"Timeline built with {len(events)} dated events from case documents")
    return events


# ------------------------------------------------------------------
# Evidence coverage
# ------------------------------------------------------------------

def evidence_coverage(case_id):
    claims = query("SELECT status FROM claims WHERE case_id = ?", (case_id,))
    total = len(claims)
    counts = {"VERIFIED": 0, "PARTIALLY SUPPORTED": 0, "UNSUPPORTED": 0,
              "CONTRADICTED": 0, "INSUFFICIENT EVIDENCE": 0}
    for c in claims:
        if c["status"] in counts:
            counts[c["status"]] += 1
    supported = counts["VERIFIED"]
    coverage = round(supported / total * 100, 1) if total else None
    return {
        "total_claims": total,
        "counts": counts,
        "coverage": coverage,
        "message": None if total >= 3 else "Not enough evaluated claims to calculate evidence coverage.",
    }


# ------------------------------------------------------------------
# Source quality
# ------------------------------------------------------------------

def source_quality(case_id):
    docs = query("SELECT * FROM documents WHERE case_id = ?", (case_id,))
    out = []
    for d in docs:
        if d["processing_status"] == "ready":
            authority, reliability = "HIGH", "Verified - text extracted and indexed"
        elif d["processing_status"] == "failed":
            authority, reliability = "LOW", "Unverified - processing failed"
        else:
            authority, reliability = "MEDIUM", "Pending processing"
        out.append({
            "document": d,
            "source_type": d["source_type"],
            "authority": authority,
            "reliability": reliability,
            "relevance": None,
            "status": d["processing_status"].upper(),
        })
    return out


# ------------------------------------------------------------------
# Missing information
# ------------------------------------------------------------------

_MISS_PATTERNS = [
    (r"paid|payment|amount|₹|rs\b|invoice|instal?ment", "Payment record / receipt",
     "The documents reference payment terms but no receipt or transaction record establishes actual payment.",
     "Upload the payment receipt or bank transaction record."),
    (r"signed|signature|executed|stamp", "Signed/execution copy",
     "Execution or signing is referenced but a signed copy is not present in the evidence.",
     "Upload the signed version of the document."),
    (r"notice|terminat", "Notice/termination correspondence",
     "Termination or notice is mentioned, but the actual notice document is not in evidence.",
     "Upload the notice or termination letter with its dispatch proof."),
    (r"identity|kyc|address", "Party identification",
     "The parties cannot be fully identified from the available documents.",
     "Upload identity/address proof of the relevant parties."),
    (r"witness|affidavit", "Witness statement / affidavit",
     "The matter references witnesses or affidavits not present in evidence.",
     "Upload the witness statement or affidavit."),
]


def detect_missing_information(case_id, store=True):
    """Derive evidence gaps from what the real documents mention but do not establish."""
    rows = query(
        "SELECT c.text, d.doc_name FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE c.case_id = ? AND d.processing_status = 'ready'", (case_id,),
    )
    corpus = "\n".join(r["text"] for r in rows)
    items = []
    for pattern, item, reason, action in _MISS_PATTERNS:
        m = re.search(pattern, corpus, re.IGNORECASE)
        if m:
            r = next(r for r in rows if re.search(pattern, r["text"], re.IGNORECASE))
            items.append({"item": item, "reason": reason, "recommended_action": action,
                          "priority": "High" if "payment" in item.lower() or "signed" in item.lower() else "Medium",
                          "context": _norm(r["text"])[:220], "doc_name": r["doc_name"]})
    if store:
        execute("DELETE FROM missing_info WHERE case_id = ?", (case_id,))
        for it in items:
            execute(
                "INSERT INTO missing_info (id, case_id, item, reason, related_claim, priority, recommended_action, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"MIS-{uuid.uuid4().hex[:10]}", case_id, it["item"], it["reason"],
                 it["context"], it["priority"], it["recommended_action"], "OPEN", _now()),
            )
        for it in items:
            log_activity(case_id, "missing_information", f"Missing information identified: {it['item']}")
    return items


# ------------------------------------------------------------------
# Case review
# ------------------------------------------------------------------

_OBLIGATION_RE = re.compile(r"\b(shall|must|agrees? to|obligat|liable|warrant(?:y|ies)?|indemnif|terminat|penalt|breach|default|confidential|arbitrat|jurisdiction|governing law)\b", re.I)
_RISK_RE = re.compile(r"\b(penalt|liabilit|breach|default|terminat|indemnif|damages?|interest|forfeit|dispute|void|rescind)\b", re.I)


def run_case_review(case_id):
    """Full review built only from real chunks. Returns structured review dict."""
    docs = query("SELECT * FROM documents WHERE case_id = ? ORDER BY upload_date", (case_id,))
    ready = [d for d in docs if d["processing_status"] == "ready"]
    if not ready:
        return {"error": "No processed documents in this case. Upload a PDF first."}

    clauses, risks, facts = [], [], []
    for d in ready:
        chunks = query(
            "SELECT * FROM chunks WHERE document_id = ? ORDER BY page_number, chunk_index", (d["id"],))
        for ch in chunks:
            first = _norm(ch["text"]).split(".")[0][:200]
            if _OBLIGATION_RE.search(ch["text"]):
                clauses.append({"doc_name": d["doc_name"], "document_id": d["id"],
                                "page": ch["page_number"], "section": ch["section"],
                                "text": _norm(ch["text"])[:400]})
            if _RISK_RE.search(ch["text"]):
                risks.append({"doc_name": d["doc_name"], "document_id": d["id"],
                              "page": ch["page_number"], "text": _norm(ch["text"])[:300],
                              "matched": _RISK_RE.search(ch["text"]).group(0).lower()})
            for s in re.split(r"(?<=[.!?])\s+", ch["text"]):
                s = _norm(s)
                if 40 < len(s) < 300 and (AMOUNT_RE.search(s) or DATE_RE.search(s)):
                    facts.append({"doc_name": d["doc_name"], "document_id": d["id"],
                                  "page": ch["page_number"], "text": s})

    contradictions = detect_contradictions(case_id)
    missing = detect_missing_information(case_id)
    extract_timeline(case_id)
    coverage = evidence_coverage(case_id)

    summary_lines = [
        f"This case contains {len(ready)} processed document(s).",
        f"{len(clauses)} obligation/clause passages and {len(risks)} risk-related passages were identified.",
        f"{len(contradictions)} contradiction(s) and {len(missing)} missing-information item(s) were detected.",
        "All content below is derived exclusively from the uploaded documents with page-level sources.",
    ]

    log_activity(case_id, "case_review", "Case review generated from real document evidence")
    return {
        "summary": " ".join(summary_lines),
        "key_facts": facts[:25],
        "clauses": clauses[:20],
        "risks": risks[:15],
        "contradictions": contradictions,
        "missing": missing,
        "coverage": coverage,
        "documents": [dict(d) for d in ready],
    }


# ------------------------------------------------------------------
# Evidence graph
# ------------------------------------------------------------------

def evidence_graph(case_id):
    """Nodes/edges: Case -> Claim -> Evidence(chunk) -> Document -> Page."""
    case = query("SELECT * FROM cases WHERE id = ?", (case_id,), one=True)
    if not case:
        return {"nodes": [], "links": []}
    nodes, links = [], []
    def node(nid, label, kind, extra=None):
        n = {"id": nid, "label": label[:60], "kind": kind}
        if extra:
            n.update(extra)
        nodes.append(n)
    node("case", case["title"], "case")
    claims = query("SELECT * FROM claims WHERE case_id = ? LIMIT 40", (case_id,))
    for c in claims:
        cid = f"claim:{c['id']}"
        node(cid, c["text"][:60], "claim", {"status": c["status"]})
        links.append({"source": "case", "target": cid, "kind": "has-claim"})
        if c["document_id"]:
            did = f"doc:{c['document_id']}"
            node(did, c["doc_name"] or "Document", "document")
            links.append({"source": cid, "target": did, "kind": "cited-in"})
            pid = f"page:{c['document_id']}:{c['page_number']}"
            node(pid, f"Page {c['page_number']}", "page")
            links.append({"source": did, "target": pid, "kind": "page"})
    return {"nodes": nodes, "links": links}
