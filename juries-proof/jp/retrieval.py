"""
JURIES PROOF - Retrieval engine.

Pure-Python BM25 over the case's real extracted chunks. Case isolation is
enforced: every query is filtered by case_id at the SQL level.
"""
import re
import math
from collections import Counter

from .db import query

_TOKEN_RE = re.compile(r"[a-z0-9₹$%.,]+", re.IGNORECASE)
STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "by", "at", "as", "it", "this", "that", "from",
    "what", "which", "who", "whom", "how", "when", "where", "why", "does", "do",
    "did", "not", "no", "but", "if", "then", "than", "so", "such", "into",
    "their", "there", "these", "those", "any", "all", "can", "will", "would",
    "shall", "should", "may", "might", "must", "have", "has", "had", "about",
}


def tokenize(text: str):
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS and len(t) > 1]


class BM25Index:
    def __init__(self, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = []       # list of dicts (chunk rows)
        self.tfs = []        # term freqs per doc
        self.doc_len = []
        self.df = Counter()
        self.avg_len = 1.0

    def add(self, chunk_row):
        tokens = tokenize(chunk_row["text"])
        tf = Counter(tokens)
        self.docs.append(chunk_row)
        self.tfs.append(tf)
        self.doc_len.append(len(tokens))
        for term in tf:
            self.df[term] += 1

    def finalize(self):
        n = len(self.docs)
        self.avg_len = (sum(self.doc_len) / n) if n else 1.0

    def search(self, query_text: str, top_k=8):
        if not self.docs:
            return []
        q_tokens = tokenize(query_text)
        if not q_tokens:
            return []
        n = len(self.docs)
        scores = []
        for i, tf in enumerate(self.tfs):
            score = 0.0
            dl = self.doc_len[i]
            for term in q_tokens:
                f = tf.get(term, 0)
                if not f:
                    continue
                df = self.df.get(term, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = f + self.k1 * (1 - self.b + self.b * dl / self.avg_len)
                score += idf * (f * (self.k1 + 1)) / denom
            scores.append((score, i))
        scores.sort(reverse=True)
        results = []
        for score, i in scores[:top_k]:
            if score <= 0:
                continue
            row = dict(self.docs[i])
            row["score"] = round(score, 4)
            results.append(row)
        return results


def _load_case_index(case_id: str) -> BM25Index:
    rows = query(
        "SELECT c.id, c.document_id, c.case_id, c.page_number, c.chunk_index, c.section, c.text, "
        "d.doc_name FROM chunks c JOIN documents d ON d.id = c.document_id WHERE c.case_id = ?",
        (case_id,),
    )
    index = BM25Index()
    for r in rows:
        index.add(r)
    index.finalize()
    return index


_index_cache = {}


def search_case(case_id: str, query_text: str, top_k=8, refresh=False):
    """Retrieve the most relevant REAL chunks for a query within one case only."""
    key = case_id
    if refresh or key not in _index_cache:
        _index_cache[key] = _load_case_index(case_id)
    index = _index_cache[key]
    return index.search(query_text, top_k=top_k)


def invalidate_case_index(case_id: str):
    _index_cache.pop(case_id, None)


def query_support(case_id: str, query_text: str, refresh=False) -> int:
    """Count distinct query content-words that exist in the case's real corpus.
    Robust small-corpus evidence gate (BM25 IDFs shrink with few docs)."""
    if refresh or case_id not in _index_cache:
        _index_cache[case_id] = _load_case_index(case_id)
    index = _index_cache[case_id]
    q = set(tokenize(query_text))
    supported = [t for t in q if index.df.get(t, 0) > 0]
    return len(supported)


def best_sentences(text: str, query_text: str, max_sentences=3):
    """Pick the sentences of a chunk most relevant to the query (for excerpts)."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]
    if not sentences:
        return [text[:300]]
    q = set(tokenize(query_text))
    scored = [(len(q & set(tokenize(s))), -len(s), s) for s in sentences]
    scored.sort(reverse=True)
    return [s for _, _, s in scored[:max_sentences]]


def split_claims(text: str):
    """Split an answer into individual factual claims (sentence level)."""
    parts = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 5]
    return parts
