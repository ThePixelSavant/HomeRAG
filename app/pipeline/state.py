"""Open-tier operational state.

One SQLite file, one writer (the ingestion worker) and N readers (the MCP
servers) -- precisely what WAL is designed for.

This owns only what Qdrant cannot answer cheaply: run history, per-source
status, per-document content hashes, and the quarantine queue. Chunk counts are
deliberately NOT tracked here; they come from Qdrant's own count(), because a
counter maintained alongside the data drifts on every partial failure and then
lies about what is actually retrievable.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    sources_run  TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    source_id        TEXT PRIMARY KEY,
    domain           TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    last_run_id      TEXT,
    last_started_at  TEXT,
    last_finished_at TEXT,
    last_status      TEXT,
    last_error       TEXT,
    last_git_sha     TEXT,
    docs_seen        INTEGER DEFAULT 0,
    chunks_upserted  INTEGER DEFAULT 0,
    chunks_deleted   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    domain        TEXT NOT NULL,
    tier          TEXT NOT NULL,
    uri           TEXT NOT NULL,
    title         TEXT,
    content_hash  TEXT NOT NULL,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    extractor     TEXT NOT NULL,
    status        TEXT NOT NULL,
    status_detail TEXT,
    mtime         REAL,
    size_bytes    INTEGER,
    last_seen_run TEXT,
    indexed_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS quarantine (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL,
    origin_path TEXT NOT NULL,
    domain      TEXT NOT NULL,
    rules       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution  TEXT
);
CREATE INDEX IF NOT EXISTS ix_quarantine_open ON quarantine(resolved_at);
"""

# Document statuses.
INDEXED = "indexed"
OCR_REQUIRED = "ocr_required"
EXTRACT_FAILED = "extract_failed"
EMPTY = "empty"
QUARANTINED = "quarantined"
QUEUED_SEALED = "queued_vault_sealed"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        # The MCP servers mount data/state read-only, so the file cannot be
        # opened read-write at all -- not just by convention, by the mount.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        return conn

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL needs a real local filesystem for its shared-memory file. A Docker
    # bind mount on ext4 qualifies; NFS would not.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def writer(path: Path | None = None):
    conn = _connect(path or settings.state_db_path, read_only=False)
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def reader(path: Path | None = None):
    """Read-only handle.

    Degrades to an empty in-memory database when the file does not exist yet or
    cannot be opened. A reader must never create or migrate the file -- the
    MCP servers mount data/state read-only on purpose -- and an index that has
    simply not been built yet should report "nothing indexed", not crash the
    server that reports it.
    """
    target = path or settings.state_db_path
    try:
        conn = _connect(target, read_only=True) if target.exists() else None
    except sqlite3.OperationalError:
        conn = None

    if conn is None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)

    try:
        yield conn
    finally:
        conn.close()


def init(path: Path | None = None) -> None:
    with writer(path):
        pass


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, run_id: str, sources: list[str]) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, started_at, status, sources_run) VALUES (?,?,?,?)",
        (run_id, utcnow(), "running", ",".join(sources)),
    )


def finish_run(conn: sqlite3.Connection, run_id: str, status: str, notes: str = "") -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, notes=? WHERE run_id=?",
        (utcnow(), status, notes, run_id),
    )


def last_runs(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def record_source_start(
    conn: sqlite3.Connection, source_id: str, domain: str, source_type: str, run_id: str
) -> None:
    conn.execute(
        """
        INSERT INTO sources (source_id, domain, source_type, last_run_id,
                             last_started_at, last_status)
        VALUES (?,?,?,?,?,'running')
        ON CONFLICT(source_id) DO UPDATE SET
            domain=excluded.domain,
            source_type=excluded.source_type,
            last_run_id=excluded.last_run_id,
            last_started_at=excluded.last_started_at,
            last_status='running',
            last_error=NULL
        """,
        (source_id, domain, source_type, run_id, utcnow()),
    )


def record_source_finish(
    conn: sqlite3.Connection,
    source_id: str,
    status: str,
    *,
    error: str | None = None,
    docs_seen: int = 0,
    chunks_upserted: int = 0,
    chunks_deleted: int = 0,
    git_sha: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE sources SET last_finished_at=?, last_status=?, last_error=?,
               docs_seen=?, chunks_upserted=?, chunks_deleted=?,
               last_git_sha=COALESCE(?, last_git_sha)
        WHERE source_id=?
        """,
        (utcnow(), status, error, docs_seen, chunks_upserted, chunks_deleted, git_sha, source_id),
    )


def get_source(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()


def all_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


def get_document(conn: sqlite3.Connection, doc_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()


def upsert_document(conn: sqlite3.Connection, **fields) -> None:
    fields.setdefault("indexed_at", utcnow())
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields if k != "doc_id")
    conn.execute(
        f"INSERT INTO documents ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(doc_id) DO UPDATE SET {updates}",
        tuple(fields.values()),
    )


def touch_document(conn: sqlite3.Connection, doc_id: str, run_id: str) -> None:
    conn.execute("UPDATE documents SET last_seen_run=? WHERE doc_id=?", (run_id, doc_id))


def delete_document(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))


def documents_missing_run(
    conn: sqlite3.Connection, source_id: str, run_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE source_id=? AND (last_seen_run IS NULL OR last_seen_run<>?)",
        (source_id, run_id),
    ).fetchall()


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) n FROM documents GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------------------
# Quarantine
# --------------------------------------------------------------------------


def add_quarantine(
    conn: sqlite3.Connection, *, path: str, origin_path: str, domain: str, rules: str, reason: str
) -> int:
    cursor = conn.execute(
        """INSERT INTO quarantine (path, origin_path, domain, rules, reason, detected_at)
           VALUES (?,?,?,?,?,?)""",
        (path, origin_path, domain, rules, reason, utcnow()),
    )
    return int(cursor.lastrowid)


def open_quarantine(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM quarantine WHERE resolved_at IS NULL ORDER BY detected_at"
    ).fetchall()


def resolve_quarantine(conn: sqlite3.Connection, quarantine_id: int, resolution: str) -> None:
    conn.execute(
        "UPDATE quarantine SET resolved_at=?, resolution=? WHERE id=?",
        (utcnow(), resolution, quarantine_id),
    )
