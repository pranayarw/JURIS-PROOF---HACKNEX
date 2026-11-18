"""
JURIES PROOF - Real PDF ingestion pipeline.

Upload -> Validate -> Store -> Parse (PyMuPDF) -> Page-by-page extraction
-> Text cleaning -> Chunking with page preservation -> Index ready.

The uploaded PDF is the source of truth. Nothing here invents content.
"""
import os
import re
import uuid
import threading
from datetime import datetime

import fitz  # PyMuPDF

from . import config
from .db import get_db, execute, log_activity

PDF_MAGIC = b"%PDF-"
MIN_TEXT_CHARS_PER_PAGE = 25  # below this a page is considered image-only


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------

def validate_pdf_file(file_storage):
    """Return (ok, error_message). Never executes the file."""
    filename = file_storage.filename or ""
    if not filename.lower().endswith(".pdf"):
        return False, "Only PDF files are supported in this version (DOCX/TXT/CSV coming soon)."
    file_storage.stream.seek(0)
    head = file_storage.stream.read(1024)
    file_storage.stream.seek(0)
    if not head.startswith(PDF_MAGIC):
        return False, "This file could not be processed as a valid PDF."
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size == 0:
        return False, "The uploaded file is empty."
    if size > config.MAX_UPLOAD_BYTES:
        return False, f"File exceeds the {config.MAX_UPLOAD_MB} MB limit."
    if len(filename) > 255:
        return False, "Filename is too long."
    return True, ""


# ------------------------------------------------------------------
# Text cleaning
# ------------------------------------------------------------------

_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_LONG_SPACES = re.compile(r"[ \t]{2,}")
_PAGE_NOISE = re.compile(r"^\s*(page\s+)?\d{1,3}\s*(of\s*\d{1,3})?\s*$", re.IGNORECASE | re.MULTILINE)


def clean_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_BREAK.sub(r"\1\2", text)          # de-hyphenate line breaks
    text = _PAGE_NOISE.sub("", text)                  # drop bare page-number lines
    text = _LONG_SPACES.sub(" ", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    return text.strip()


# ------------------------------------------------------------------
# Section detection (heuristic, from the real text only)
# ------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^(?:(?:clause|article|section|schedule|annex(?:ure)?)\s+[0-9IVXivx]+[A-Za-z]?[.:)]?|[0-9]{1,2}(?:\.[0-9]+)*[.:)]\s+\S.{0,80})\s*$",
    re.IGNORECASE,
)

def looks_like_section(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 100:
        return False
    return bool(_SECTION_RE.match(line))


# ------------------------------------------------------------------
# Chunking (page-preserving)
# ------------------------------------------------------------------

def chunk_page_text(text: str, max_chars=config.CHUNK_SIZE_CHARS, overlap=config.CHUNK_OVERLAP_CHARS):
    """Split one page's text into overlapping chunks on paragraph boundaries."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []
    chunks, buf = [], ""
    for para in paragraphs:
        if len(para) > max_chars:
            # split very long paragraphs on sentence boundaries
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for s in sentences:
                if len(buf) + len(s) + 1 >= max_chars and buf:
                    chunks.append(buf.strip())
                    buf = buf[-overlap:] if overlap else ""
                buf += (" " if buf else "") + s
        else:
            if len(buf) + len(para) + 2 >= max_chars and buf:
                chunks.append(buf.strip())
                buf = buf[-overlap:] if overlap else ""
            buf += ("\n\n" if buf else "") + para
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if len(c) > 20]


# ------------------------------------------------------------------
# Full ingestion (runs in background thread)
# ------------------------------------------------------------------

def ingest_document(document_id: str):
    """Process a stored PDF: extract pages, clean, chunk, index. Never invents text."""
    doc_row = None
    conn = get_db()
    doc_row = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    conn.close()
    if doc_row is None:
        return

    def set_status(status, error=None, page_count=None):
        execute(
            "UPDATE documents SET processing_status = ?, processing_error = ?, "
            "last_processed = ?, page_count = COALESCE(?, page_count) WHERE id = ?",
            (status, error, datetime.utcnow().isoformat(), page_count, document_id),
        )

    set_status("processing")
    path = doc_row["file_path"]
    case_id = doc_row["case_id"]

    try:
        if not os.path.exists(path):
            raise RuntimeError("Stored file is missing.")
        pdf = fitz.open(path)
        if pdf.needs_pass:
            raise RuntimeError("Password-protected PDFs cannot be processed.")
    except RuntimeError as e:
        set_status("failed", str(e))
        log_activity(case_id, "document_failed", f"{doc_row['doc_name']}: {e}")
        return
    except Exception:
        set_status("failed", "Document processing failed. Please verify that the PDF is valid and readable.")
        log_activity(case_id, "document_failed", f"{doc_row['doc_name']}: invalid PDF")
        return

    try:
        conn = get_db()
        conn.execute("DELETE FROM pages WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        conn.commit()
        conn.close()

        set_status("extracting")
        total_text_chars = 0
        rows_pages, rows_chunks = [], []
        for page_num0 in range(pdf.page_count):
            page = pdf.load_page(page_num0)
            raw = page.get_text("text")
            text = clean_text(raw)
            total_text_chars += len(text)
            section = None
            for line in text.splitlines():
                if looks_like_section(line):
                    section = line.strip()
                    break
            rows_pages.append((document_id, case_id, page_num0 + 1, text, len(text), section))

        set_status("indexing")
        for (doc_id, c_id, page_number, text, _cc, section) in rows_pages:
            conn = get_db()
            conn.execute(
                "INSERT INTO pages (document_id, case_id, page_number, text, char_count, section) VALUES (?,?,?,?,?,?)",
                (doc_id, c_id, page_number, text, len(text), section),
            )
            conn.commit()
            conn.close()

        for (doc_id, c_id, page_number, text, _cc, section) in rows_pages:
            for idx, chunk in enumerate(chunk_page_text(text)):
                chunk_id = f"{doc_id}-p{page_number}-c{idx}"
                rows_chunks.append((chunk_id, doc_id, c_id, page_number, idx, section, chunk))

        if rows_chunks:
            conn = get_db()
            conn.executemany(
                "INSERT INTO chunks (id, document_id, case_id, page_number, chunk_index, section, text) VALUES (?,?,?,?,?,?,?)",
                rows_chunks,
            )
            conn.commit()
            conn.close()

        scanned = total_text_chars < (MIN_TEXT_CHARS_PER_PAGE * pdf.page_count)
        if scanned:
            if config.OCR_ENABLED:
                set_status("failed",
                           "This document appears to be scanned. OCR is required for full text extraction "
                           "and is not yet configured.")
            else:
                set_status("failed",
                           "INSUFFICIENT DOCUMENT TEXT: This document appears to be scanned. "
                           "OCR is required to extract searchable text.")
            log_activity(case_id, "document_failed", f"{doc_row['doc_name']}: appears scanned (OCR needed)")
            pdf.close()
            return

        page_count_final = pdf.page_count
        set_status("ready", None, page_count=page_count_final)
        pdf.close()
        log_activity(case_id, "document_indexed",
                     f"{doc_row['doc_name']} processed: {page_count_final} pages, {len(rows_chunks)} evidence chunks indexed")
    except Exception:
        set_status("failed", "Text could not be extracted from this document.")
        log_activity(case_id, "document_failed", f"{doc_row['doc_name']}: extraction failure")


def start_ingestion(document_id: str):
    thread = threading.Thread(target=ingest_document, args=(document_id,), daemon=True)
    thread.start()
    return thread
