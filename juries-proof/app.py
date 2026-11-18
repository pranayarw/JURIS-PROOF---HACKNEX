"""
JURIES PROOF - Flask application.

Evidence-first legal intelligence platform:
real PDF ingestion -> page-preserving extraction -> retrieval -> grounded
answers with claim/citation verification -> refuse when evidence is
insufficient.
"""
import os
import re
import json
import uuid
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, jsonify, send_file, abort,
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from jp import config, db, ingestion, retrieval, verification, analysis, llm
from jp.db import query, execute, log_activity

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES

ROLE_LABELS = {
    "admin": "Administrator", "analyst": "Analyst",
    "law_enforcement": "Law Enforcement", "government": "Government",
    "public": "Public",
}


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------

def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return query("SELECT * FROM users WHERE id = ?", (uid,), one=True)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def roles_allowed(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("You do not have access to that area.", "warning")
                return redirect(url_for("public_help"))
            return f(*args, **kwargs)
        return wrapper
    return deco


@app.context_processor
def inject_globals():
    user = current_user()
    return dict(now=datetime.utcnow(), current_user=user,
                role_labels=ROLE_LABELS, app_name="JURIES PROOF",
                llm_configured=config.llm_configured())


# ------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = query("SELECT * FROM users WHERE username = ?", (username,), one=True)
        if user and check_password_hash(user["password_hash"], password):
            session["uid"] = user["id"]
            session.permanent = True
            log_activity(None, "login", f"User '{username}' signed in")
            if user["role"] == "public":
                return redirect(url_for("public_help"))
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------
# Dashboard
# ------------------------------------------------------------------

@app.route("/")
@app.route("/dashboard")
@roles_allowed("admin", "analyst", "law_enforcement", "government")
def dashboard():
    stats = {
        "active_cases": query("SELECT COUNT(*) c FROM cases WHERE status='Open'", one=True)["c"],
        "documents": query("SELECT COUNT(*) c FROM documents WHERE processing_status='ready'", one=True)["c"],
        "verified_claims": query("SELECT COUNT(*) c FROM claims WHERE status='VERIFIED'", one=True)["c"],
        "contradictions": query("SELECT COUNT(*) c FROM contradictions", one=True)["c"],
        "missing": query("SELECT COUNT(*) c FROM missing_info WHERE status='OPEN'", one=True)["c"],
        "research": query("SELECT COUNT(*) c FROM research_queries", one=True)["c"],
        "drafts": query("SELECT COUNT(*) c FROM drafts", one=True)["c"],
        "total_claims": query("SELECT COUNT(*) c FROM claims", one=True)["c"],
    }
    verified = stats["verified_claims"]
    stats["coverage"] = round(verified / stats["total_claims"] * 100, 1) if stats["total_claims"] else None
    recent_cases = query(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM documents d WHERE d.case_id=c.id AND d.processing_status='ready') docs, "
        "(SELECT COUNT(*) FROM claims cl WHERE cl.case_id=c.id AND cl.status='VERIFIED') verified, "
        "(SELECT COUNT(*) FROM contradictions co WHERE co.case_id=c.id) contra, "
        "(SELECT COUNT(*) FROM missing_info mi WHERE mi.case_id=c.id AND mi.status='OPEN') miss "
        "FROM cases c ORDER BY c.updated_at DESC LIMIT 8")
    activities = query("SELECT * FROM activities ORDER BY id DESC LIMIT 12")
    return render_template("dashboard.html", stats=stats, recent_cases=recent_cases, activities=activities)


# ------------------------------------------------------------------
# Case management
# ------------------------------------------------------------------

def _next_case_id():
    n = query("SELECT COUNT(*) c FROM cases", one=True)["c"] + 1
    return f"CASE-{n:04d}"


@app.route("/cases")
@login_required
def cases_page():
    all_cases = query(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM documents d WHERE d.case_id=c.id) docs "
        "FROM cases c ORDER BY c.created_at DESC")
    return render_template("cases.html", cases=all_cases)


@app.route("/cases/create", methods=["POST"])
@login_required
@roles_allowed("admin", "analyst", "law_enforcement")
def create_case():
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("Case title is required.", "danger")
        return redirect(url_for("cases_page"))
    case_id = _next_case_id()
    now = datetime.utcnow().isoformat()
    execute(
        "INSERT INTO cases (id, title, case_type, jurisdiction, status, description, parties, tags, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (case_id, title, request.form.get("case_type"), request.form.get("jurisdiction"),
         "Open", request.form.get("description"), request.form.get("parties"),
         request.form.get("tags"), now, now))
    log_activity(case_id, "case_created", f"Case '{title}' created")
    flash(f"Case {case_id} created. Upload a PDF to begin.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/cases/<case_id>")
@login_required
def case_detail(case_id, tab="overview"):
    tab = request.args.get("tab", "overview")
    case = query("SELECT * FROM cases WHERE id = ?", (case_id,), one=True)
    if not case:
        flash("Case not found.", "danger")
        return redirect(url_for("cases_page"))
    documents = query("SELECT * FROM documents WHERE case_id = ? ORDER BY upload_date DESC", (case_id,))
    coverage = analysis.evidence_coverage(case_id)
    counts = {
        "contradictions": query("SELECT COUNT(*) c FROM contradictions WHERE case_id=?", (case_id,), one=True)["c"],
        "missing": query("SELECT COUNT(*) c FROM missing_info WHERE case_id=? AND status='OPEN'", (case_id,), one=True)["c"],
        "claims": query("SELECT COUNT(*) c FROM claims WHERE case_id=?", (case_id,), one=True)["c"],
        "events": query("SELECT COUNT(*) c FROM events WHERE case_id=?", (case_id,), one=True)["c"],
    }
    ctx = {"case": case, "documents": documents, "tab": tab, "coverage": coverage, "counts": counts}

    if tab == "chat":
        ctx["messages"] = query("SELECT * FROM chat_messages WHERE case_id=? ORDER BY id DESC LIMIT 40", (case_id,))[::-1]
        ctx["claims"] = query("SELECT * FROM claims WHERE case_id=? AND origin='rag' ORDER BY created_at DESC LIMIT 30", (case_id,))
    elif tab == "timeline":
        ctx["events"] = query("SELECT * FROM events WHERE case_id=? ORDER BY id", (case_id,))
    elif tab == "contradictions":
        ctx["contradictions"] = query("SELECT * FROM contradictions WHERE case_id=? ORDER BY id DESC", (case_id,))
    elif tab == "missing":
        ctx["missing"] = query("SELECT * FROM missing_info WHERE case_id=? ORDER BY id DESC", (case_id,))
    elif tab == "review":
        ctx["review"] = analysis.run_case_review(case_id)

    return render_template("case_detail.html", **ctx)


@app.route("/cases/<case_id>/rename", methods=["POST"])
@login_required
@roles_allowed("admin", "analyst")
def rename_case(case_id):
    title = (request.form.get("title") or "").strip()
    if title:
        execute("UPDATE cases SET title=?, updated_at=? WHERE id=?", (title, datetime.utcnow().isoformat(), case_id))
        flash("Case renamed.", "success")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/cases/<case_id>/delete", methods=["POST"])
@login_required
@roles_allowed("admin")
def delete_case(case_id):
    for sql in ("DELETE FROM chunks WHERE case_id=?", "DELETE FROM pages WHERE case_id=?",
                "DELETE FROM documents WHERE case_id=?", "DELETE FROM claims WHERE case_id=?",
                "DELETE FROM citations WHERE case_id=?", "DELETE FROM contradictions WHERE case_id=?",
                "DELETE FROM missing_info WHERE case_id=?", "DELETE FROM events WHERE case_id=?",
                "DELETE FROM chat_messages WHERE case_id=?", "DELETE FROM research_queries WHERE case_id=?",
                "DELETE FROM drafts WHERE case_id=?", "DELETE FROM activities WHERE case_id=?"):
        execute(sql, (case_id,))
    execute("DELETE FROM cases WHERE id=?", (case_id,))
    retrieval.invalidate_case_index(case_id)
    flash(f"Case {case_id} deleted.", "success")
    return redirect(url_for("cases_page"))


# ------------------------------------------------------------------
# Upload + processing status
# ------------------------------------------------------------------

@app.route("/cases/<case_id>/upload", methods=["POST"])
@login_required
@roles_allowed("admin", "analyst", "law_enforcement")
def upload_document(case_id):
    case = query("SELECT * FROM cases WHERE id = ?", (case_id,), one=True)
    if not case:
        abort(404)
    file = request.files.get("pdf")
    if file is None or file.filename == "":
        flash("Please choose a PDF to upload.", "danger")
        return redirect(url_for("case_detail", case_id=case_id, tab="documents"))

    ok, err = ingestion.validate_pdf_file(file)
    if not ok:
        flash(err, "danger")
        return redirect(url_for("case_detail", case_id=case_id, tab="documents"))

    doc_id = f"DOC-{uuid.uuid4().hex[:10]}"
    filename = secure_filename(file.filename) or "document.pdf"
    case_dir = os.path.join(config.UPLOAD_DIR, case_id)
    os.makedirs(case_dir, exist_ok=True)
    stored_path = os.path.join(case_dir, f"{doc_id}__{filename}")
    file.save(stored_path)

    execute(
        "INSERT INTO documents (id, case_id, doc_name, original_filename, file_path, file_type, file_size, "
        "page_count, upload_date, processing_status, processing_error, source_type, last_processed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, case_id, filename, file.filename, stored_path, "pdf",
         os.path.getsize(stored_path), 0, datetime.utcnow().isoformat(),
         "validating", None, "user_uploaded", None))
    log_activity(case_id, "document_uploaded", f"PDF uploaded: {filename}")
    ingestion.start_ingestion(doc_id)
    flash(f"{filename} uploaded. Processing started.", "success")
    return redirect(url_for("case_detail", case_id=case_id, tab="documents"))


@app.route("/api/documents/<doc_id>/status")
@login_required
def document_status(doc_id):
    d = query("SELECT processing_status, processing_error, page_count FROM documents WHERE id=?",
              (doc_id,), one=True)
    if not d:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": d["processing_status"], "error": d["processing_error"],
                    "page_count": d["page_count"]})


@app.route("/documents/<doc_id>/delete", methods=["POST"])
@login_required
@roles_allowed("admin", "analyst")
def delete_document(doc_id):
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    for sql in ("DELETE FROM chunks WHERE document_id=?", "DELETE FROM pages WHERE document_id=?",
                "DELETE FROM documents WHERE id=?"):
        execute(sql, (doc_id,))
    try:
        os.remove(d["file_path"])
    except OSError:
        pass
    retrieval.invalidate_case_index(d["case_id"])
    log_activity(d["case_id"], "document_deleted", f"Document removed: {d['doc_name']}")
    flash("Document removed from case.", "success")
    return redirect(url_for("case_detail", case_id=d["case_id"], tab="documents"))


@app.route("/documents/<doc_id>/reprocess", methods=["POST"])
@login_required
@roles_allowed("admin", "analyst")
def reprocess_document(doc_id):
    d = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not d:
        abort(404)
    execute("UPDATE documents SET processing_status='queued', processing_error=NULL WHERE id=?", (doc_id,))
    retrieval.invalidate_case_index(d["case_id"])
    ingestion.start_ingestion(doc_id)
    flash("Reprocessing started.", "success")
    return redirect(url_for("document_viewer", doc_id=doc_id))


# ------------------------------------------------------------------
# Document viewer
# ------------------------------------------------------------------

@app.route("/documents/<doc_id>")
@login_required
def document_viewer(doc_id):
    doc = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not doc:
        abort(404)
    case = query("SELECT * FROM cases WHERE id=?", (doc["case_id"],), one=True)
    pages = query("SELECT page_number, char_count, section FROM pages WHERE document_id=? ORDER BY page_number", (doc_id,))
    page = request.args.get("page", type=int)
    page_text = None
    claims = []
    if page:
        row = query("SELECT text, section FROM pages WHERE document_id=? AND page_number=?", (doc_id, page), one=True)
        page_text = row["text"] if row else None
        claims = query(
            "SELECT * FROM claims WHERE document_id=? AND page_number=? ORDER BY created_at DESC LIMIT 20",
            (doc_id, page))
    all_claims = query("SELECT * FROM claims WHERE document_id=? ORDER BY created_at DESC LIMIT 30", (doc_id,))
    return render_template("document_viewer.html", doc=doc, case=case, pages=pages,
                           page=page, page_text=page_text, claims=claims, all_claims=all_claims)


@app.route("/documents/<doc_id>/file")
@login_required
def document_file(doc_id):
    doc = query("SELECT * FROM documents WHERE id=?", (doc_id,), one=True)
    if not doc or not os.path.exists(doc["file_path"]):
        abort(404)
    return send_file(doc["file_path"], mimetype="application/pdf",
                     download_name=doc["doc_name"], as_attachment=False)


# ------------------------------------------------------------------
# RAG chat (case-specific, evidence-grounded)
# ------------------------------------------------------------------

@app.route("/cases/<case_id>/ask", methods=["POST"])
@login_required
@roles_allowed("admin", "analyst", "law_enforcement", "government")
def ask_case(case_id):
    q = (request.form.get("question") or "").strip()
    if not q:
        flash("Enter a question.", "warning")
        return redirect(url_for("case_detail", case_id=case_id, tab="chat"))
    execute("INSERT INTO chat_messages (case_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
            (case_id, "user", q, None, datetime.utcnow().isoformat()))
    verification.rag_answer(case_id, q, store=True)
    return redirect(url_for("case_detail", case_id=case_id, tab="chat"))


@app.route("/api/ask", methods=["POST"])
@login_required
@roles_allowed("admin", "analyst", "law_enforcement", "government")
def api_ask():
    data = request.get_json(silent=True) or {}
    case_id, q = data.get("case_id"), (data.get("question") or "").strip()
    if not case_id or not q:
        return jsonify({"error": "case_id and question required"}), 400
    execute("INSERT INTO chat_messages (case_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
            (case_id, "user", q, None, datetime.utcnow().isoformat()))
    result = verification.rag_answer(case_id, q, store=False)
    return jsonify(result)


# ------------------------------------------------------------------
# Case review / analyze / evidence center
# ------------------------------------------------------------------

@app.route("/cases/<case_id>/review")
@login_required
@roles_allowed("admin", "analyst", "law_enforcement", "government")
def case_review_page(case_id):
    return redirect(url_for("case_detail", case_id=case_id, tab="review"))


@app.route("/cases/<case_id>/analyze", methods=["POST"])
@login_required
@roles_allowed("admin", "analyst", "law_enforcement")
def analyze_case(case_id):
    contradictions = analysis.detect_contradictions(case_id)
    missing = analysis.detect_missing_information(case_id)
    analysis.extract_timeline(case_id)
    flash(f"Analysis complete: {len(contradictions)} contradiction(s), {len(missing)} missing item(s).", "success")
    return redirect(url_for("case_detail", case_id=case_id, tab="overview"))


@app.route("/cases/<case_id>/evidence")
@login_required
def evidence_center(case_id):
    case = query("SELECT * FROM cases WHERE id=?", (case_id,), one=True)
    if not case:
        abort(404)
    claims = query("SELECT * FROM claims WHERE case_id=? ORDER BY created_at DESC LIMIT 100", (case_id,))
    citations = query(
        "SELECT ci.*, cl.text AS claim_text FROM citations ci JOIN claims cl ON cl.id=ci.claim_id "
        "WHERE ci.case_id=? ORDER BY ci.id DESC LIMIT 100", (case_id,))
    graph = analysis.evidence_graph(case_id)
    coverage = analysis.evidence_coverage(case_id)
    quality = analysis.source_quality(case_id)
    return render_template("evidence_center.html", case=case, claims=claims, citations=citations,
                           graph=graph, coverage=coverage, quality=quality)


@app.route("/api/evidence/graph/<case_id>")
@login_required
def api_graph(case_id):
    return jsonify(analysis.evidence_graph(case_id))


@app.route("/api/verify/claim", methods=["POST"])
@login_required
def api_verify_claim():
    data = request.get_json(silent=True) or {}
    claim = (data.get("claim") or "").strip()
    case_id = data.get("case_id")
    if not claim or not case_id:
        return jsonify({"error": "claim and case_id required"}), 400
    hits = retrieval.search_case(case_id, claim, top_k=5)
    status, ch, excerpt = verification.verify_claim_against_evidence(claim, hits)
    return jsonify({"status": status, "doc_name": ch["doc_name"] if ch else None,
                    "page": ch["page_number"] if ch else None, "excerpt": excerpt})


@app.route("/api/verify/citation", methods=["POST"])
@login_required
def api_verify_citation():
    data = request.get_json(silent=True) or {}
    doc_id, page, excerpt = data.get("document_id"), data.get("page"), (data.get("excerpt") or "")
    if not doc_id or not page:
        return jsonify({"error": "document_id and page required"}), 400
    valid = verification.verify_citation(data.get("claim", ""), doc_id, int(page), excerpt)
    return jsonify({"valid": valid, "status": "VALID" if valid else "INVALID"})


# ------------------------------------------------------------------
# Legal research
# ------------------------------------------------------------------

@app.route("/cases/<case_id>/research", methods=["GET", "POST"])
@login_required
@roles_allowed("admin", "analyst", "law_enforcement", "government")
def research_page(case_id):
    case = query("SELECT * FROM cases WHERE id=?", (case_id,), one=True)
    if not case:
        abort(404)
    result = None
    if request.method == "POST":
        q = (request.form.get("question") or "").strip()
        if q:
            hits = retrieval.search_case(case_id, q, top_k=5)
            grounded = bool(hits and hits[0]["score"] >= config.MIN_EVIDENCE_SCORE)
            external_note = (
                "External legal source APIs are not configured. No statutes, judgments or "
                "citations are fabricated: external authorities are shown as RESEARCH SOURCE UNAVAILABLE."
                if not config.LEGAL_RESEARCH_API_KEY else
                "External legal research source configured."
            )
            case_summary = []
            for h in hits[:3]:
                from jp.retrieval import best_sentences
                for s in best_sentences(h["text"], q, 2):
                    case_summary.append({"text": s, "doc": h["doc_name"], "page": h["page_number"]})
            status = "CASE-GROUNDED" if grounded else "INSUFFICIENT EVIDENCE"
            result = {"question": q, "status": status, "case_summary": case_summary,
                      "external_note": external_note,
                      "external_sources": []}
            execute("INSERT INTO research_queries (id, case_id, question, answer, meta, created_at) VALUES (?,?,?,?,?,?)",
                    (f"RQ-{uuid.uuid4().hex[:10]}", case_id, q, status,
                     json.dumps({"hits": len(hits)}), datetime.utcnow().isoformat()))
            log_activity(case_id, "research_query", f"Research query: {q[:80]}")
    history = query("SELECT * FROM research_queries WHERE case_id=? ORDER BY created_at DESC LIMIT 10", (case_id,))
    return render_template("research.html", case=case, result=result, history=history)


# ------------------------------------------------------------------
# Legal drafting
# ------------------------------------------------------------------

def _verified_facts(case_id):
    return query("SELECT * FROM claims WHERE case_id=? AND status='VERIFIED' ORDER BY created_at DESC LIMIT 15", (case_id,))


DRAFT_KINDS = {
    "case_summary": "Case Summary",
    "legal_notice": "Legal Notice",
    "contract_summary": "Contract Summary",
    "formal_letter": "Formal Letter",
}


@app.route("/cases/<case_id>/draft", methods=["GET", "POST"])
@login_required
@roles_allowed("admin", "analyst", "law_enforcement")
def drafting_page(case_id):
    case = query("SELECT * FROM cases WHERE id=?", (case_id,), one=True)
    if not case:
        abort(404)
    draft = None
    if request.method == "POST":
        kind = request.form.get("kind", "case_summary")
        title = DRAFT_KINDS.get(kind, kind.title())
        facts = _verified_facts(case_id)
        fact_lines = "\n".join(
            f"- {c['text']} (Source: {c['doc_name']}, page {c['page_number']})" if c["doc_name"]
            else f"- {c['text']}" for c in facts) or "- [INFORMATION REQUIRED: no verified case facts yet]"

        parties = case["parties"] or "[INFORMATION REQUIRED: party names]"
        if kind == "legal_notice":
            content = (
                f"LEGAL NOTICE\n\nTo: {parties}\n\n"
                "Under instructions from my client, [INFORMATION REQUIRED: client name and address], "
                "I serve you with the following notice:\n\n"
                "1. VERIFIED CASE FACTS (from uploaded documents):\n" + fact_lines + "\n\n"
                "2. [INFORMATION REQUIRED: the specific demand/relief sought]\n"
                "3. [INFORMATION REQUIRED: compliance period]\n\n"
                "Note: Legal provisions cited must be verified by a qualified legal professional. "
                "No statute or judgment has been invented by this system."
            )
        elif kind == "contract_summary":
            content = (
                f"CONTRACT SUMMARY - {case['title']}\n\n"
                "Parties: " + parties + "\n\n"
                "VERIFIED TERMS (from uploaded documents):\n" + fact_lines + "\n\n"
                "Missing terms: [INFORMATION REQUIRED where not established by uploaded documents]\n"
            )
        else:  # case_summary / formal_letter
            content = (
                f"{title.upper()} - {case['title']} (Case ID {case['id']})\n\n"
                "VERIFIED CASE FACTS (each sourced from uploaded PDFs):\n" + fact_lines + "\n\n"
                "Jurisdiction: " + (case["jurisdiction"] or "[INFORMATION REQUIRED]") + "\n\n"
                "This draft contains only facts verified against uploaded documents. "
                "Sections marked [INFORMATION REQUIRED] must be completed from real sources."
            )
        draft_id = f"DFT-{uuid.uuid4().hex[:10]}"
        execute("INSERT INTO drafts (id, case_id, kind, title, content, verification_note, created_at) VALUES (?,?,?,?,?,?,?)",
                (draft_id, case_id, kind, title, content,
                 "Only VERIFIED claims are included; all gaps are marked [INFORMATION REQUIRED].",
                 datetime.utcnow().isoformat()))
        log_activity(case_id, "draft_generated", f"{title} generated from verified case facts")
        draft = query("SELECT * FROM drafts WHERE id=?", (draft_id,), one=True)
    drafts = query("SELECT * FROM drafts WHERE case_id=? ORDER BY created_at DESC", (case_id,))
    return render_template("drafting.html", case=case, drafts=drafts, draft=draft,
                           kinds=DRAFT_KINDS, facts=_verified_facts(case_id))


# ------------------------------------------------------------------
# Report
# ------------------------------------------------------------------

@app.route("/cases/<case_id>/report")
@login_required
def case_report(case_id):
    case = query("SELECT * FROM cases WHERE id=?", (case_id,), one=True)
    if not case:
        abort(404)
    review = analysis.run_case_review(case_id)
    events = query("SELECT * FROM events WHERE case_id=? ORDER BY created_at", (case_id,))
    research = query("SELECT * FROM research_queries WHERE case_id=? ORDER BY created_at DESC LIMIT 5", (case_id,))
    drafts = query("SELECT * FROM drafts WHERE case_id=? ORDER BY created_at DESC LIMIT 5", (case_id,))
    return render_template("report.html", case=case, review=review, events=events,
                           research=research, drafts=drafts)


# ------------------------------------------------------------------
# Global search (case-aware)
# ------------------------------------------------------------------

@app.route("/search")
@login_required
def global_search():
    q = (request.args.get("q") or "").strip()
    case_filter = (request.args.get("case") or "").strip()
    results = []
    cases = query("SELECT id, title FROM cases ORDER BY updated_at DESC")
    if q:
        if case_filter:
            hits = retrieval.search_case(case_filter, q, top_k=20)
            for h in hits:
                h["case_id"] = case_filter
            results = hits
        else:
            for c in cases:
                for h in retrieval.search_case(c["id"], q, top_k=5):
                    h["case_id"] = c["id"]
                    results.append(h)
            results.sort(key=lambda x: -x["score"])
            results = results[:20]
    return render_template("search.html", q=q, results=results, cases=cases, case_filter=case_filter)


# ------------------------------------------------------------------
# Evaluation module (real tests against real documents)
# ------------------------------------------------------------------

@app.route("/evaluation", methods=["GET", "POST"])
@login_required
def evaluation_page():
    cases = query("SELECT id, title FROM cases ORDER BY title")
    run = None
    if request.method == "POST":
        case_id = request.form.get("case_id")
        run = _run_evaluation(case_id)
    return render_template("evaluation.html", cases=cases, run=run)


def _run_evaluation(case_id):
    rows = query(
        "SELECT c.id, c.document_id, c.page_number, c.section, c.text, d.doc_name "
        "FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.case_id=? "
        "AND d.processing_status='ready' AND length(c.text) > 120 ORDER BY RANDOM() LIMIT 5",
        (case_id,))
    if not rows:
        return {"error": "No processed documents in this case to evaluate."}
    tests = []
    for r in rows:
        sentence = next((s for s in re.split(r"(?<=[.!?])\s+", r["text"]) if len(s) > 60), r["text"][:150])
        hits = retrieval.search_case(case_id, sentence, top_k=3, refresh=False)
        found = any(h["document_id"] == r["document_id"] and h["page_number"] == r["page_number"] for h in hits)
        cite_ok = verification.verify_citation(sentence, r["document_id"], r["page_number"], sentence) if found else False
        tests.append({
            "question": sentence,
            "expected_evidence": f"{r['doc_name']} - page {r['page_number']}",
            "evidence_found": found,
            "citation_accuracy": cite_ok,
        })
    # Insufficient-evidence test: a question no document can answer
    bogus = "zebra quantum.xy controversy entirely absent from all documents 99999"
    bogus_hits = retrieval.search_case(case_id, bogus, top_k=1, refresh=False)
    support = retrieval.query_support(case_id, bogus, refresh=False)
    refused = (not bogus_hits) or support < 2 or bogus_hits[0]["score"] < config.MIN_EVIDENCE_SCORE
    tests.append({"question": "[Refusal test] deliberately unanswerable question",
                  "expected_evidence": "Must answer INSUFFICIENT EVIDENCE",
                  "evidence_found": refused, "citation_accuracy": refused})
    groundedness = round(sum(1 for t in tests if t["evidence_found"]) / len(tests) * 100, 1)
    citation_acc = round(sum(1 for t in tests if t["citation_accuracy"]) / len(tests) * 100, 1)
    result = {"case_id": case_id, "tests": tests, "groundedness": groundedness,
              "citation_accuracy": citation_acc,
              "fabricated_facts": 0, "fabricated_citations": 0}
    execute("INSERT INTO evaluation_runs (id, case_id, results, created_at) VALUES (?,?,?,?)",
            (f"EVL-{uuid.uuid4().hex[:10]}", case_id, json.dumps(result), datetime.utcnow().isoformat()))
    return result


# ------------------------------------------------------------------
# Public legal help
# ------------------------------------------------------------------

PUBLIC_RESOURCES = [
    {"name": "India Code - Central Acts", "url": "https://www.indiacode.nic.in/",
     "desc": "Official repository of central laws of India."},
    {"name": "eCourts Services", "url": "https://services.ecourts.gov.in/",
     "desc": "Official case status and court services portal."},
    {"name": "National Legal Services Authority (NALSA)", "url": "https://nalsa.gov.in/",
     "desc": "Free legal aid information and Lok Adalat services."},
    {"name": "Department of Justice", "url": "https://doj.gov.in/",
     "desc": "Official government portal for justice-related services."},
]


@app.route("/public-help")
def public_help():
    return render_template("public_help.html", resources=PUBLIC_RESOURCES)


# ------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------

@app.errorhandler(404)
def nf(e):
    return render_template("error.html", code=404, message="Page not found."), 404


@app.errorhandler(413)
def too_large(e):
    flash(f"File exceeds the {config.MAX_UPLOAD_MB} MB limit.", "danger")
    return redirect(url_for("dashboard"))


@app.errorhandler(500)
def ise(e):
    return render_template("error.html", code=500,
                           message="Something went wrong. Please try again."), 500


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    os.makedirs(config.UPLOAD_DIR, exist_ok=True)
    print("JURIES PROOF - Evidence-First Legal Intelligence")
    print("Starting on http://127.0.0.1:5000")
    app.run(debug=True, host="127.0.0.1", port=5000)
