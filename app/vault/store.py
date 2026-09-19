"""Encrypted vault storage.

Everything sensitive lives here rather than in Qdrant, because a vector cannot
be encrypted and remain searchable, and inversion reconstructs most of the
source text from an embedding. Putting a receipt's vector in Qdrant with an
encrypted payload would protect the text field while the vector leaked its
contents.

Semantic search still works: vectors are stored as BLOBs inside the encrypted
database and brute-forced in memory while unlocked. At ~10k chunks x 384
float32 that is ~15 MB and sub-millisecond, which comfortably covers six
figures of chunks. FTS5 rides along for keyword search, and the ledger answers
aggregation in exact SQL.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import numpy as np

VECTOR_DTYPE = np.float32

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id         TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL,
    domain         TEXT NOT NULL,
    uri            TEXT NOT NULL,
    title          TEXT,
    content_hash   TEXT NOT NULL,
    chunk_count    INTEGER NOT NULL DEFAULT 0,
    extractor      TEXT NOT NULL,
    status         TEXT NOT NULL,
    status_detail  TEXT,
    raw_extraction TEXT,
    mtime          REAL,
    size_bytes     INTEGER,
    last_seen_run  TEXT,
    indexed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_vdocs_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS ix_vdocs_domain ON documents(domain);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    domain       TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL,
    text         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector       BLOB NOT NULL,
    token_count  INTEGER,
    heading_path TEXT,
    indexed_at   TEXT,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_vchunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS ix_vchunks_domain ON chunks(domain);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, chunk_id UNINDEXED, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    domain         TEXT NOT NULL,
    txn_date       TEXT NOT NULL,
    merchant       TEXT,
    amount_cents   INTEGER NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    category       TEXT,
    payment_method TEXT,
    description    TEXT,
    line_items     TEXT,
    confidence     REAL,
    extracted_by   TEXT,
    needs_review   INTEGER NOT NULL DEFAULT 0,
    review_notes   TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE(doc_id, txn_date, amount_cents, merchant)
);
CREATE INDEX IF NOT EXISTS ix_ledger_date ON ledger(txn_date);
CREATE INDEX IF NOT EXISTS ix_ledger_merchant ON ledger(merchant);
CREATE INDEX IF NOT EXISTS ix_ledger_review ON ledger(needs_review);

CREATE TABLE IF NOT EXISTS blobs (
    doc_id       TEXT PRIMARY KEY,
    rel_path     TEXT NOT NULL,
    content_type TEXT,
    size_bytes   INTEGER,
    nonce        BLOB NOT NULL,
    sha256       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    principal     TEXT,
    transport     TEXT NOT NULL,
    chat_id       TEXT,
    message_id    TEXT,
    tool          TEXT NOT NULL,
    query_hash    TEXT,
    decision      TEXT NOT NULL,
    reason        TEXT,
    rows_returned INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def pack_vector(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=VECTOR_DTYPE).tobytes()


def unpack_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=VECTOR_DTYPE)


# --------------------------------------------------------------------------
# Documents and chunks
# --------------------------------------------------------------------------


def get_document(conn, doc_id: str):
    return conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()


def upsert_document(conn, **fields) -> None:
    fields.setdefault("indexed_at", utcnow())
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields if k != "doc_id")
    conn.execute(
        f"INSERT INTO documents ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(doc_id) DO UPDATE SET {updates}",
        tuple(fields.values()),
    )


def touch_document(conn, doc_id: str, run_id: str) -> None:
    conn.execute("UPDATE documents SET last_seen_run=? WHERE doc_id=?", (run_id, doc_id))


def replace_chunks(conn, doc_id: str, rows: list[dict]) -> None:
    """Replace a document's chunks wholesale.

    The open tier needs positional tombstones because Qdrant has no cascade;
    here a DELETE plus FTS cleanup is exact and cheap, so shrinkage is handled
    by construction rather than by a separate sweep.
    """
    old = [r["chunk_id"] for r in conn.execute(
        "SELECT chunk_id FROM chunks WHERE doc_id=?", (doc_id,)
    ).fetchall()]
    if old:
        conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", [(c,) for c in old])
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    conn.executemany(
        """INSERT INTO chunks (chunk_id, doc_id, domain, chunk_index, text,
                               content_hash, vector, token_count, heading_path, indexed_at)
           VALUES (:chunk_id,:doc_id,:domain,:chunk_index,:text,:content_hash,
                   :vector,:token_count,:heading_path,:indexed_at)""",
        rows,
    )
    conn.executemany(
        "INSERT INTO chunks_fts (text, chunk_id) VALUES (:text, :chunk_id)",
        [{"text": r["text"], "chunk_id": r["chunk_id"]} for r in rows],
    )
    conn.execute(
        "UPDATE documents SET chunk_count=? WHERE doc_id=?", (len(rows), doc_id)
    )


def delete_document(conn, doc_id: str) -> None:
    old = [r["chunk_id"] for r in conn.execute(
        "SELECT chunk_id FROM chunks WHERE doc_id=?", (doc_id,)
    ).fetchall()]
    if old:
        conn.executemany("DELETE FROM chunks_fts WHERE chunk_id=?", [(c,) for c in old])
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))


def documents_missing_run(conn, source_id: str, run_id: str):
    return conn.execute(
        "SELECT * FROM documents WHERE source_id=? AND (last_seen_run IS NULL OR last_seen_run<>?)",
        (source_id, run_id),
    ).fetchall()


def counts_by_domain(conn) -> dict[str, int]:
    rows = conn.execute("SELECT domain, COUNT(*) n FROM chunks GROUP BY domain").fetchall()
    return {r["domain"]: r["n"] for r in rows}


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def search_semantic(
    conn, query_vector: list[float], *, domains: list[str] | None = None, limit: int = 5
) -> list[dict]:
    """Brute-force cosine over the decrypted vectors.

    No ANN index: at this scale the whole matrix is a few tens of MB, a single
    dot product is sub-millisecond, and an on-disk ANN structure would be one
    more thing holding derived plaintext.
    """
    sql = "SELECT c.*, d.uri, d.title FROM chunks c JOIN documents d USING(doc_id)"
    params: list[Any] = []
    if domains:
        sql += f" WHERE c.domain IN ({','.join('?' * len(domains))})"
        params.extend(domains)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []

    matrix = np.vstack([unpack_vector(r["vector"]) for r in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.where(norms == 0, 1, norms)

    query = np.asarray(query_vector, dtype=VECTOR_DTYPE)
    query_norm = np.linalg.norm(query)
    if query_norm:
        query = query / query_norm

    scores = matrix @ query
    top = np.argsort(-scores)[:limit]
    return [_hit(rows[i], float(scores[i])) for i in top]


def search_keyword(
    conn, query: str, *, domains: list[str] | None = None, limit: int = 5
) -> list[dict]:
    sql = """
        SELECT c.*, d.uri, d.title, bm25(chunks_fts) AS rank
        FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
        JOIN documents d USING(doc_id)
        WHERE chunks_fts MATCH ?
    """
    params: list[Any] = [query]
    if domains:
        sql += f" AND c.domain IN ({','.join('?' * len(domains))})"
        params.extend(domains)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # FTS5 raises on malformed match syntax; an unparseable query is not an
        # error worth failing the whole search over.
        return []
    return [_hit(r, -float(r["rank"])) for r in rows]


def _hit(row, score: float) -> dict:
    return {
        "score": score,
        "content": row["text"],
        "title": row["title"],
        "uri": row["uri"],
        "domain": row["domain"],
        "doc_id": row["doc_id"],
        "chunk_index": row["chunk_index"],
        "heading_path": json.loads(row["heading_path"] or "[]"),
        "indexed_at": row["indexed_at"],
    }
