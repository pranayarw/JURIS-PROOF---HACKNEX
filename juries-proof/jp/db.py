"""
JURIES PROOF - Database layer (SQLite).

All data is real: cases, uploaded documents, extracted pages/chunks,
claims, citations, contradictions, missing information, events, activity.
"""
import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "storage", "juries_proof.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'analyst',
    display_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    case_type TEXT,
    jurisdiction TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    description TEXT,
    parties TEXT,
    tags TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    doc_name TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_type TEXT,
    file_size INTEGER,
    page_count INTEGER DEFAULT 0,
    upload_date TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'uploaded',
    processing_error TEXT,
    source_type TEXT DEFAULT 'user_uploaded',
    last_processed TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    text TEXT,
    char_count INTEGER DEFAULT 0,
    section TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    section TEXT,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UNSUPPORTED',
    origin TEXT DEFAULT 'rag',
    document_id TEXT,
    doc_name TEXT,
    page_number INTEGER,
    section TEXT,
    excerpt TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS citations (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    document_id TEXT,
    doc_name TEXT,
    page_number INTEGER,
    section TEXT,
    excerpt TEXT,
    valid INTEGER DEFAULT 0,
    status TEXT DEFAULT 'UNVERIFIED'
);

CREATE TABLE IF NOT EXISTS contradictions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    topic TEXT,
    claim_a TEXT,
    claim_b TEXT,
    doc_a TEXT, page_a INTEGER,
    doc_b TEXT, page_b INTEGER,
    excerpt_a TEXT,
    excerpt_b TEXT,
    status TEXT DEFAULT 'DETECTED',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missing_info (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    item TEXT NOT NULL,
    reason TEXT,
    related_claim TEXT,
    priority TEXT DEFAULT 'Medium',
    recommended_action TEXT,
    status TEXT DEFAULT 'OPEN',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    event_date TEXT,
    description TEXT NOT NULL,
    document_id TEXT,
    doc_name TEXT,
    page_number INTEGER,
    excerpt TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_queries (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    meta TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    verification_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    results TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legal_sources (
    id TEXT PRIMARY KEY,
    case_id TEXT,
    name TEXT NOT NULL,
    source_type TEXT,
    authority TEXT,
    reliability TEXT,
    relevance REAL,
    status TEXT DEFAULT 'UNVERIFIED',
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_case ON chunks(case_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_pages_doc ON pages(document_id);
CREATE INDEX IF NOT EXISTS idx_claims_case ON claims(case_id);
"""


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    _seed_users(conn)
    conn.close()


def _seed_users(conn):
    from werkzeug.security import generate_password_hash
    now = datetime.utcnow().isoformat()
    seed = [
        ("admin", "admin123", "admin", "System Administrator"),
        ("analyst", "analyst123", "analyst", "Legal Analyst"),
        ("leo", "leo123456", "law_enforcement", "Law Enforcement Officer"),
        ("gov", "gov123456", "government", "Government Reviewer"),
        ("public", "public123", "public", "Public User"),
    ]
    for username, pwd, role, name in seed:
        cur = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, display_name, created_at) VALUES (?,?,?,?,?)",
                (username, generate_password_hash(pwd), role, name, now),
            )
    conn.commit()


def query(sql, params=(), one=False):
    conn = get_db()
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return (rows[0] if rows else None) if one else rows


def execute(sql, params=()):
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    lastrowid, rowcount = cur.lastrowid, cur.rowcount
    conn.close()
    return lastrowid, rowcount


def executemany(sql, seq):
    conn = get_db()
    conn.executemany(sql, seq)
    conn.commit()
    conn.close()


def log_activity(case_id, type_, description):
    execute(
        "INSERT INTO activities (case_id, type, description, created_at) VALUES (?,?,?,?)",
        (case_id, type_, description, datetime.utcnow().isoformat()),
    )
