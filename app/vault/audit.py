"""Append-only record of every vault access attempt, allowed or denied.

This is both the visibility into what has been read and the detector for a
model probing at the boundary: a run of DENIED rows from a legitimate identity
is what prompt injection looks like from this side.

Denials are the important rows, and they occur precisely when the vault may be
sealed -- so when the encrypted log is unreachable the event still goes to the
journal rather than being dropped.
"""

from __future__ import annotations

import logging

from app.vault.store import utcnow

logger = logging.getLogger("vault.audit")

ALLOWED = "allowed"
DENIED = "denied"
PENDING = "pending_approval"


def record(
    conn,
    *,
    tool: str,
    decision: str,
    transport: str,
    principal: str | None = None,
    chat_id: str | None = None,
    message_id: str | None = None,
    query_hash: str | None = None,
    reason: str | None = None,
    rows_returned: int = 0,
) -> None:
    conn.execute(
        """INSERT INTO audit (ts, principal, transport, chat_id, message_id, tool,
                              query_hash, decision, reason, rows_returned)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            utcnow(),
            principal,
            transport,
            chat_id,
            message_id,
            tool,
            query_hash,
            decision,
            reason,
            rows_returned,
        ),
    )
    conn.commit()


def record_safe(conn_factory, **fields) -> None:
    """Audit without letting a logging failure mask the event.

    If the vault is sealed there is no encrypted log to write to, which is
    exactly when a denial is most worth knowing about.
    """
    try:
        conn = conn_factory()
    except Exception:
        logger.warning(
            "VAULT %s tool=%s principal=%s chat=%s reason=%s (vault sealed; journal only)",
            fields.get("decision"),
            fields.get("tool"),
            fields.get("principal"),
            fields.get("chat_id"),
            fields.get("reason"),
        )
        return
    try:
        record(conn, **fields)
    finally:
        conn.close()


def tail(conn, limit: int = 50):
    return conn.execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
