# JURIES PROOF

### Every legal claim. Every source. Every proof.

JURIES PROOF is an **evidence-first legal intelligence prototype**: upload real PDFs, get page-preserving text extraction, search the extracted evidence, ask grounded questions, and receive answers that are verified, cited to a real page, or refused with **INSUFFICIENT EVIDENCE** rather than invented.

> **Central principle:** If the system cannot find sufficient evidence, it must not invent the answer.

```
Retrieve → Analyze → Verify → Cite → Explain → Refuse when evidence is insufficient
```

---

## Features

| Area | What works |
|---|---|
| **Real PDF ingestion** | Upload → validate (magic bytes, size) → PyMuPDF page-by-page extraction → text cleaning → section detection → page-preserving chunking → BM25 index. Works 100% locally, no API keys. |
| **Processing status** | Live statuses: validating → processing → extracting → indexing → ready / failed. Scanned PDFs are reported as `INSUFFICIENT DOCUMENT TEXT` (OCR optional, never fabricated). |
| **Document viewer** | Page navigation, embedded PDF at the chosen page, extracted page text, page-linked claims with **Why?** expansion. |
| **Grounded RAG chat** | Case-scoped BM25 retrieval → optional LLM synthesis (strictly constrained to evidence) → claim extraction → claim verification → citation verification → answer or refusal. Without an LLM key, extractive answers built from real evidence are used. |
| **Claim verification** | VERIFIED / PARTIALLY SUPPORTED / UNSUPPORTED / CONTRADICTED / INSUFFICIENT EVIDENCE — computed by comparing claims' dates, amounts, entities against retrieved evidence. |
| **Citation verification** | A citation is VALID only if its excerpt actually occurs on the stored page of the real PDF. |
| **Contradiction detection** | Cross-document date/amount conflicts, both sources shown, no arbitrary winner. |
| **Missing information** | Evidence gaps derived from what documents reference but do not establish, with recommended actions. |
| **Timeline** | Dated events extracted from real pages, each with document + page source. |
| **Evidence Center** | Evidence graph (Case→Claim→Document→Page), coverage %, citation verification table, source quality. |
| **Case review / reports** | Full review and print-friendly report from real data only. |
| **Legal research** | Case-grounded analysis; external authorities are never fabricated (RESEARCH SOURCE UNAVAILABLE). |
| **Legal drafting** | Drafts include only VERIFIED facts; all gaps marked `[INFORMATION REQUIRED]`. |
| **Search** | Global, case-aware evidence search with page links. |
| **Evaluation** | Tests retrieval on real documents + a deliberate refusal test; no fabricated scores. |
| **Auth & roles** | Login, logout, role-based access (admin / analyst / law enforcement / government / public). Case isolation is enforced at every query. |

---

## Quick start

```bash
cd juries-proof
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000** and sign in (prototype accounts, change before real deployment):

| Username | Password | Role |
|---|---|---|
| admin | admin123 | Administrator |
| analyst | analyst123 | Analyst |
| leo | leo123456 | Law Enforcement |
| gov | gov123456 | Government |
| public | public123 | Public (Public Legal Help only) |

Optional LLM/OCR keys: copy `.env.example`, set variables in your environment. The app is fully functional without them.

---

## Project structure

```
juries-proof/
├── app.py                  # Flask routes
├── jp/
│   ├── config.py           # Environment / limits
│   ├── db.py               # SQLite schema + helpers
│   ├── ingestion.py        # PDF validation + PyMuPDF pipeline (background)
│   ├── retrieval.py        # BM25 over case chunks (case-isolated)
│   ├── llm.py              # Gemini/OpenAI abstraction (optional)
│   ├── verification.py     # Claim + citation verification, RAG pipeline
│   └── analysis.py         # Contradictions, timeline, coverage, review, graph
├── templates/              # 14 professional templates
├── static/
│   ├── css/style.css       # Light/dark legal-tech theme
│   ├── js/main.js          # Theme, dropzone, status polling, SVG graph
│   └── img/logo.png        # Branding + favicon
├── storage/                # SQLite DB + uploaded PDFs (gitignored)
├── .env.example
└── requirements.txt
```

---

## Acceptance test (do this yourself)

1. Create a case, upload a **new PDF the app has never seen** containing a unique fact (name, amount, date).
2. Open **Documents** → wait for `READY`.
3. **Chat tab** → ask about that fact → answer must show document name, real page number and actual excerpt with **Verified** status.
4. Ask about something **not in the PDF** → `INSUFFICIENT EVIDENCE` (no invented answer).
5. Upload a second PDF contradicting the first → **Analyze Case** → *Contradictions* shows both sources, no winner chosen.
6. **Evaluation** page → run on the case → groundedness/refusal metrics computed from those real tests.

---

## Disclaimer

JURIES PROOF is a prototype for legal document analysis, research assistance, and evidence organization. It does not provide legal advice, does not replace a qualified legal professional, and should not be relied upon as a substitute for professional legal judgment or official legal authorities.
