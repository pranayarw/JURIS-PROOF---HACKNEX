"""
JURIES PROOF - Generation and verification are SEPARATE.

RAG pipeline:
  Question -> Retrieve real chunks -> (optional LLM synthesis | extractive answer)
  -> Extract claims -> Verify claims vs evidence -> Verify citations vs pages
  -> Answer + citations + statuses, or INSUFFICIENT EVIDENCE.

Nothing is marked VERIFIED merely because a citation exists: citation
verification re-checks the excerpt against the actual page text stored
from the uploaded PDF.
"""
import re
import uuid
from datetime import datetime

from . import config
from .db import query, execute, log_activity
from . import retrieval
from .llm import generate, GROUNDING_SYSTEM, LLMUnavailable

DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*,?\s*\d{0,4}\b"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*\d{0,4})",
    re.IGNORECASE,
)
AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr|usd|\$)\s?[\d,]+(?:\.\d+)?", re.IGNORECASE)
PCT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def key_elements(claim: str):
    """Numbers, dates, percentages and capitalized entities a claim depends on."""
    return {
        "dates": [d.strip() for d in DATE_RE.findall(claim)],
        "amounts": [a.strip() for a in AMOUNT_RE.findall(claim)],
        "pcts": [p.strip() for p in PCT_RE.findall(claim)],
        "entities": [
            w for w in re.findall(r"\b[A-Z][a-zA-Z]{2,}\b", claim)
            if w.lower() not in STOPWORDS_CAP
        ][:6],
    }


STOPWORDS_CAP = {
    "the", "this", "that", "these", "those", "and", "but", "for", "with", "from",
    "page", "section", "evidence", "payment", "agreement", "contract", "document",
    "parties", "party", "terms", "under", "shall", "insufficient",
}


def verify_claim_against_evidence(claim: str, evidence_chunks):
    """Compare a claim to real evidence chunks. Returns (status, best_chunk, excerpt)."""
    claim_norm = _norm(claim)
    claim_tokens = set(retrieval.tokenize(claim))
    claim_keys = key_elements(claim)

    best, best_overlap, best_excerpt = None, 0.0, ""
    for ch in evidence_chunks:
        text_norm = _norm(ch["text"])
        overlap = len(claim_tokens & set(retrieval.tokenize(ch["text"])))
        if overlap > best_overlap:
            best, best_overlap = ch, overlap
            best_excerpt = ch["text"]

    if best is None:
        return "INSUFFICIENT EVIDENCE", None, ""

    ev_norm = _norm(best["text"])

    def found(items, field_norm):
        return all(_norm(i) in field_norm for i in items)

    key_hits, key_total = 0, 0
    for d in claim_keys["dates"] + claim_keys["amounts"] + claim_keys["pcts"]:
        key_total += 1
        if _norm(d) in ev_norm:
            key_hits += 1
    for ent in claim_keys["entities"]:
        key_total += 1
        if ent.lower() in ev_norm:
            key_hits += 1

    contradicting = _find_contradiction(claim, claim_keys, evidence_chunks, best)

    if key_total and key_hits == 0:
        return ("CONTRADICTED" if contradicting else "UNSUPPORTED"), best, best_excerpt
    if best_overlap >= max(3, len(claim_tokens) * 0.4) and (key_total == 0 or key_hits == key_total):
        return "VERIFIED", best, best_excerpt
    if best_overlap >= 2:
        return ("PARTIALLY SUPPORTED" if not contradicting else "CONTRADICTED"), best, best_excerpt
    return ("CONTRADICTED" if contradicting else "INSUFFICIENT EVIDENCE"), best, best_excerpt


def _find_contradiction(claim, claim_keys, evidence_chunks, best):
    """Does other evidence state a different date/amount for the same topic?"""
    if best is None:
        return False
    best_tokens = set(retrieval.tokenize(best["text"]))
    for ch in evidence_chunks:
        if ch["id"] == best["id"]:
            continue
        other_tokens = set(retrieval.tokenize(ch["text"]))
        shared = best_tokens & other_tokens
        if len(shared) < 6:
            continue
        for d in claim_keys["dates"]:
            if _norm(d) not in _norm(ch["text"]):
                od = DATE_RE.findall(ch["text"])
                if od and _norm(od[0]) != _norm(d):
                    return True
        for a in claim_keys["amounts"]:
            if _norm(a) not in _norm(ch["text"]):
                oa = AMOUNT_RE.findall(ch["text"])
                if oa and _norm(oa[0]) != _norm(a):
                    return True
    return False


def verify_citation(claim_text: str, document_id: str, page_number: int, excerpt: str):
    """A citation is valid ONLY if the excerpt actually occurs on that real page."""
    page = query(
        "SELECT text FROM pages WHERE document_id = ? AND page_number = ?",
        (document_id, page_number), one=True,
    )
    if page is None:
        return False
    return _norm(excerpt)[:400] in _norm(page["text"]) or _norm(page["text"]) in _norm(claim_text)


# ------------------------------------------------------------------
# RAG answer pipeline
# ------------------------------------------------------------------

def rag_answer(case_id: str, question: str, store=True):
    docs = query("SELECT COUNT(*) AS c FROM documents WHERE case_id = ? AND processing_status = 'ready'", (case_id,), one=True)
    if not docs or docs["c"] == 0:
        return _refusal(case_id, question, "no_docs", store)

    hits = retrieval.search_case(case_id, question, top_k=6, refresh=False)
    support = retrieval.query_support(case_id, question)
    # Refuse unless at least 2 distinct content words of the question exist
    # in the case's real corpus (1 support = incidental term overlap).
    if not hits or support < 2 or hits[0]["score"] < config.MIN_EVIDENCE_SCORE:
        return _refusal(case_id, question, "insufficient", store, closest=hits)

    # --- generation (strictly grounded, evidence-numbered) ---
    evidence_blocks = []
    for i, h in enumerate(hits, 1):
        evidence_blocks.append(
            f"[E{i}] Document: {h['doc_name']} | Page: {h['page_number']} | Section: {h['section'] or 'N/A'}\n{h['text']}"
        )
    evidence_text = "\n\n".join(evidence_blocks)
    llm_text, used_llm = None, False
    try:
        llm_text = generate(
            f"EVIDENCE:\n{evidence_text}\n\nQUESTION: {question}\n\n"
            "Answer using only the evidence. Cite [E1], [E2]... If insufficient, reply INSUFFICIENT EVIDENCE.",
            GROUNDING_SYSTEM,
        )
        used_llm = True
    except LLMUnavailable:
        used_llm = False

    if used_llm and "INSUFFICIENT EVIDENCE" in llm_text.upper()[:60]:
        return _refusal(case_id, question, "insufficient", store, closest=hits)

    if used_llm:
        answer_text = llm_text.strip()
    else:
        # Extractive fallback: compose answer ONLY from the best real sentences.
        from .retrieval import best_sentences
        parts = []
        for h in hits[:3]:
            for s in best_sentences(h["text"], question, 2):
                cite = f"{h['doc_name']}, page {h['page_number']}"
                parts.append(f"{s} (Source: {cite})")
        answer_text = " ".join(parts)

    # --- verification: extract claims, verify each, verify citations ---
    # Strip source markers so they are not mistaken for factual claims.
    clean_answer = re.sub(r"\s*\(Source:\s*[^)]*\)", "", answer_text)
    claims = retrieval.split_claims(clean_answer)
    verified_claims = []
    for c in claims:
        if not _is_factual(c):
            continue
        status, ch, excerpt = verify_claim_against_evidence(c, hits)
        claim_id = f"CLM-{uuid.uuid4().hex[:10]}"
        doc_id = ch["document_id"] if ch else None
        page_no = ch["page_number"] if ch else None
        section = ch["section"] if ch else None
        doc_name = ch["doc_name"] if ch else None
        cite_valid = False
        if ch is not None and excerpt:
            cite_valid = verify_citation(c, doc_id, page_no, excerpt)
        execute(
            "INSERT INTO claims (id, case_id, text, status, origin, document_id, doc_name, page_number, section, excerpt, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (claim_id, case_id, c, status, "rag", doc_id, doc_name, page_no, section, excerpt[:600], datetime.utcnow().isoformat()),
        )
        if ch is not None:
            execute(
                "INSERT INTO citations (id, claim_id, case_id, document_id, doc_name, page_number, section, excerpt, valid, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"CIT-{uuid.uuid4().hex[:10]}", claim_id, case_id, doc_id, doc_name, page_no, section,
                 excerpt[:600], 1 if cite_valid else 0, "VALID" if cite_valid else "INVALID"),
            )
        verified_claims.append({
            "text": c, "status": status, "doc_name": doc_name, "page": page_no,
            "section": section, "excerpt": excerpt, "citation_valid": cite_valid,
            "claim_id": claim_id,
        })

    overall = _overall_status(verified_claims)
    result = {
        "question": question,
        "answer": answer_text,
        "llm_used": used_llm,
        "status": overall,
        "claims": verified_claims,
        "evidence": [
            {"doc_name": h["doc_name"], "page": h["page_number"], "section": h["section"],
             "text": h["text"], "score": h["score"], "document_id": h["document_id"]}
            for h in hits
        ],
        "refusal": False,
    }
    if store:
        execute(
            "INSERT INTO chat_messages (case_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
            (case_id, "assistant", answer_text,
             _json({"status": overall, "claims": verified_claims, "llm_used": used_llm}),
             datetime.utcnow().isoformat()),
        )
        log_activity(case_id, "rag_query", f"Evidence question answered: {question[:80]}")
    return result


def _is_factual(sentence: str) -> bool:
    s = sentence.strip().lower()
    if len(s) < 30:
        return False
    filler = s.startswith(("hello", "based on the available", "i am", "i can"))
    return not filler and not s.startswith("source:")


def _overall_status(claims):
    if not claims:
        return "INSUFFICIENT EVIDENCE"
    statuses = {c["status"] for c in claims}
    if "CONTRADICTED" in statuses:
        return "CONTRADICTED"
    if "UNSUPPORTED" in statuses:
        return "UNSUPPORTED"
    if "PARTIALLY SUPPORTED" in statuses:
        return "PARTIALLY SUPPORTED"
    if "VERIFIED" in statuses:
        return "VERIFIED"
    return "INSUFFICIENT EVIDENCE"


def _refusal(case_id, question, kind, store, closest=None):
    closest_ev = [
        {"doc_name": h["doc_name"], "page": h["page_number"], "section": h["section"],
         "text": h["text"], "score": h["score"], "document_id": h["document_id"]}
        for h in (closest or [])[:3]
    ]
    result = {
        "question": question,
        "answer": None,
        "status": "INSUFFICIENT EVIDENCE",
        "refusal": True,
        "refusal_kind": kind,
        "message": (
            "No documents have been uploaded to this case yet. Upload a PDF to build evidence."
            if kind == "no_docs" else
            "The available documents do not provide sufficient evidence to answer this question. "
            "No answer has been invented."
        ),
        "evidence": closest_ev,
        "missing": _suggest_missing(question),
        "llm_used": False,
        "claims": [],
    }
    if store:
        execute(
            "INSERT INTO chat_messages (case_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
            (case_id, "assistant", result["message"], _json({"status": "INSUFFICIENT EVIDENCE", "refusal": True}),
             datetime.utcnow().isoformat()),
        )
        log_activity(case_id, "insufficient_evidence", f"Refused (no fabricated answer): {question[:80]}")
    return result


def _suggest_missing(question):
    q = question.lower()
    items = []
    if any(w in q for w in ("paid", "payment", "amount", "₹", "rs", "transfer")):
        items += ["Payment record / receipt", "Bank transaction record"]
    if any(w in q for w in ("signed", "signature", "executed")):
        items += ["Signed agreement / execution copy"]
    if any(w in q for w in ("who", "identity", "party", "parties")):
        items += ["Party identification documents"]
    if any(w in q for w in ("when", "date", "timeline")):
        items += ["Correspondence establishing the relevant date"]
    return items[:4] or ["A document that directly establishes the requested fact"]


def _json(obj):
    import json
    return json.dumps(obj, default=str)
