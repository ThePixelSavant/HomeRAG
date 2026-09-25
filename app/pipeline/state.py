"""Open-tier operational state.

One SQLite file, one writer (the ingestion worker) and N readers (the MCP
servers). It uses the default rollback journal, not WAL, because the readers
mount data/state read-only -- see `_connect`.

This owns only what Qdrant cannot answer cheaply: run history, per-source
status, per-document content hashes, and the quarantine queue. Chunk counts are
deliberately NOT tracked here; they come from Qdrant's own count(), because a
counter maintained alongside the data drifts on every partial failure and then
lies about what is actually retrievable.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app import documents
from app.config import settings

logger = logging.getLogger(__name__)

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
    indexed_at    TEXT,
    -- Lifecycle is SEPARATE from `status` above. `status` says whether the
    -- pipeline could read the file; lifecycle says whether its contents should
    -- still be believed. Merging them would make "extractable" and "true" the
    -- same question.
    lifecycle        TEXT NOT NULL DEFAULT 'active',
    lifecycle_reason TEXT,
    lifecycle_set_at TEXT,
    effective_date   TEXT,
    review_after     TEXT,
    supersedes       TEXT,
    superseded_by    TEXT,
    -- Pages an extractor flagged as possibly misread, for review or the Phase
    -- 2 VLM pass. The PDF extractor no longer sets it (ADR-024); the column
    -- stays for whatever flags pages next.
    flagged_pages    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents(status);
-- ix_documents_lifecycle is created in _migrate, not here. On an existing
-- database CREATE TABLE IF NOT EXISTS is a no-op, so the column above does
-- not exist yet and indexing it here would fail the whole script.

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

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a
# no-op against an existing file, so a new column in SCHEMA above never reaches
# a database that already exists -- it has to be added explicitly.
_ADDED_COLUMNS: dict[str, str] = {
    "lifecycle": "TEXT NOT NULL DEFAULT 'active'",
    "lifecycle_reason": "TEXT",
    "lifecycle_set_at": "TEXT",
    "effective_date": "TEXT",
    "review_after": "TEXT",
    "supersedes": "TEXT",
    "superseded_by": "TEXT",
    "flagged_pages": "INTEGER NOT NULL DEFAULT 0",
}

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
    # Rollback journal, NOT WAL. A WAL file can only be opened read-only if its
    # -wal/-shm files exist or can be created, and the worker deletes them when
    # it exits -- after which the MCP servers' `:ro` mount cannot recreate them
    # and every state read fails. With one occasional writer, WAL's concurrent
    # reads buy nothing a 5s busy timeout doesn't. Set on every open because the
    # mode is persisted in the file: this is what converts an old WAL database.
    # synchronous stays at its default, FULL: NORMAL is only safe under WAL.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns missing from an older `documents` table."""
    have = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    if not have:
        return
    for column, decl in _ADDED_COLUMNS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_documents_lifecycle ON documents(lifecycle)")


@contextmanager
def writer(path: Path | None = None):
    conn = _connect(path or settings.state_db_path, read_only=False)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
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
    conn = None
    if target.exists():
        try:
            conn = _connect(target, read_only=True)
            # sqlite3.connect() opens lazily; a file that cannot actually be
            # read (not a database, or a hot journal left by a crashed writer
            # that a read-only handle cannot roll back) only fails here.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError:
            logger.warning("state db %s is unreadable; reporting it as empty", target)
            if conn is not None:
                conn.close()
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
    """Rows this run did not touch -- excluding tombstones.

    A retracted document is deliberately skipped before extraction, so it never
    gets this run's marker. Without the exclusion the sweep would delete the
    very row that keeps it out, and the next run would index it again.
    """
    placeholders = ",".join("?" for _ in documents.TOMBSTONED)
    return conn.execute(
        f"SELECT * FROM documents WHERE source_id=? "
        f"AND (last_seen_run IS NULL OR last_seen_run<>?) "
        f"AND lifecycle NOT IN ({placeholders})",
        (source_id, run_id, *sorted(documents.TOMBSTONED)),
    ).fetchall()


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) n FROM documents GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def count_by_lifecycle(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts per lifecycle, or {} against a database predating the column.

    Readers cannot migrate: the MCP servers mount data/state read-only on
    purpose, so they would crash on a schema the ingestion worker has not
    caught up to yet. Degrading is the only safe direction -- the alternative
    is a server that will not start until an unrelated container has run.
    """
    try:
        rows = conn.execute(
            "SELECT lifecycle, COUNT(*) n FROM documents GROUP BY lifecycle"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["lifecycle"]: r["n"] for r in rows}


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def find_by_uri(conn: sqlite3.Connection, uri: str, domain: str | None = None) -> list[sqlite3.Row]:
    """Documents whose uri matches exactly, or ends with `uri` as a path suffix.

    The CLI takes the path the user actually typed, which is usually the tail
    of a longer rel_uri.
    """
    sql = "SELECT * FROM documents WHERE (uri=? OR uri LIKE ?)"
    params: list = [uri, f"%/{uri}"]
    if domain:
        sql += " AND domain=?"
        params.append(domain)
    return conn.execute(sql + " ORDER BY uri", params).fetchall()


def set_lifecycle(
    conn: sqlite3.Connection,
    doc_id: str,
    lifecycle: str,
    *,
    reason: str | None = None,
    superseded_by: str | None = None,
    supersedes: str | None = None,
    review_after: str | None = None,
) -> None:
    documents.validate(lifecycle)
    conn.execute(
        """UPDATE documents SET lifecycle=?, lifecycle_reason=?, lifecycle_set_at=?,
                  superseded_by=COALESCE(?, superseded_by),
                  supersedes=COALESCE(?, supersedes),
                  review_after=COALESCE(?, review_after)
           WHERE doc_id=?""",
        (lifecycle, reason, utcnow(), superseded_by, supersedes, review_after, doc_id),
    )


def schedule_review(conn: sqlite3.Connection, doc_id: str, review_after: str) -> None:
    """Set only the review date.

    Deliberately NOT set_lifecycle with an unchanged lifecycle: that overwrites
    `lifecycle_reason` with whatever was passed, so scheduling a review on an
    already-flagged document would erase the note saying why it was flagged.
    """
    conn.execute(
        "UPDATE documents SET review_after=? WHERE doc_id=?", (review_after, doc_id)
    )


def clear_review(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute("UPDATE documents SET review_after=NULL WHERE doc_id=?", (doc_id,))


def tombstones(conn: sqlite3.Connection, source_id: str | None = None) -> dict[str, sqlite3.Row]:
    """{doc_id: row} for documents that must not be re-indexed."""
    placeholders = ",".join("?" for _ in documents.TOMBSTONED)
    sql = f"SELECT * FROM documents WHERE lifecycle IN ({placeholders})"
    params: list = list(sorted(documents.TOMBSTONED))
    if source_id:
        sql += " AND source_id=?"
        params.append(source_id)
    return {row["doc_id"]: row for row in conn.execute(sql, params)}


def flagged_pages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Documents with pages flagged for review.

    Tombstoned documents are left out: a retracted document is never re-read,
    so its count is frozen at whatever the extractor said before it was
    retracted, and there is nothing to review.
    """
    placeholders = ",".join("?" for _ in documents.TOMBSTONED)
    try:
        return conn.execute(
            "SELECT uri, domain, flagged_pages FROM documents WHERE flagged_pages > 0 "
            f"AND lifecycle NOT IN ({placeholders}) ORDER BY flagged_pages DESC",
            sorted(documents.TOMBSTONED),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def due_for_review(conn: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    """Active documents whose review date has passed."""
    return conn.execute(
        "SELECT * FROM documents WHERE lifecycle=? AND review_after IS NOT NULL "
        "AND review_after <= ?",
        (documents.ACTIVE, today),
    ).fetchall()


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
