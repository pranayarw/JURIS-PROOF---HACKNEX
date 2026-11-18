"""JURIES PROOF - configuration and environment handling."""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
UPLOAD_DIR = os.path.join(STORAGE_DIR, "uploads")

MAX_UPLOAD_MB = 50
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
ALLOWED_MIME = {"application/pdf"}

CHUNK_SIZE_CHARS = 1200
CHUNK_OVERLAP_CHARS = 150

# Retrieval thresholds. The primary evidence gate is query_support() >= 2
# (distinct content words present in the case's real corpus). The BM25 score
# floor is kept low because raw BM25 scores shrink in small corpora.
MIN_EVIDENCE_SCORE = 0.3
MIN_EVIDENCE_SCORE_STRICT = 1.5

SECRET_KEY = os.environ.get("SECRET_KEY", "juries-proof-dev-secret-change-me")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
OCR_API_KEY = os.environ.get("OCR_API_KEY", "")
LEGAL_RESEARCH_API_KEY = os.environ.get("LEGAL_RESEARCH_API_KEY", "")

OCR_ENABLED = bool(OCR_API_KEY)


def llm_configured():
    return bool(GEMINI_API_KEY or OPENAI_API_KEY)
