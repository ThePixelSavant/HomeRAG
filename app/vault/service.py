"""The three gates, applied in order, in one place.

Every model-initiated read of vault data passes all three:

  1. the vault is unlocked      -- else there is no key and no plaintext
  2. the caller's identity verifies against Open WebUI's signed assertion
  3. a human approved THIS request, bound to its message id, single use

The CLI path (a human typing at a terminal) skips only gate 3, because a human
is already in the loop. Claude Code gets no exemption: it is a model, so it
passes all three, exactly as Open WebUI does.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.domains import Tier, UnknownDomainError, domains_in, tier_of
from app.pipeline import embedder
from app.vault import audit, blobs, grants, identity, store
from app.vault.crypto import VaultSealed
from app.vault.keyagent import AGENT

logger = logging.getLogger(__name__)

CLI = "cli"
MCP = "mcp"

# Columns query_ledger may group by or filter on. Fixed whitelist: an
# LLM-driven tool that accepted free-form SQL against a file of financial
# history is not a thing to build.
LEDGER_FILTERS = {"merchant", "category", "payment_method", "currency"}
LEDGER_GROUPS = {"month": "substr(txn_date,1,7)", "merchant": "merchant", "category": "category"}


@dataclass
class Context:
    transport: str = CLI
    headers: dict[str, str] = field(default_factory=dict)
    chat_id: str = ""
    message_id: str = ""

    @property
    def requires_approval(self) -> bool:
        return self.transport != CLI


def _conn():
    return AGENT.connect()


def _resolve_domains(requested: list[str] | None) -> list[str]:
    allowed = set(domains_in(Tier.VAULT))
    if not requested:
        return sorted(allowed)
    resolved = []
    for domain in requested:
        try:
            if tier_of(domain) is not Tier.VAULT:
                raise PermissionError(
                    f"{domain!r} is an open-tier domain; query it with search_docs instead."
                )
        except UnknownDomainError as exc:
            raise PermissionError(str(exc)) from None
        resolved.append(domain)
    return resolved


def _gate(ctx: Context, tool: str, payload: str) -> tuple[str, str]:
    """Run gates 1-3. Returns (principal_label, query_hash)."""
    qhash = grants.query_hash(tool, payload)

    # Gate 1: sealed means sealed. Checked first so a sealed vault never even
    # reveals whether an identity would have been accepted.
    if not AGENT.is_unlocked():
        audit.record_safe(
            _conn,
            tool=tool,
            decision=audit.DENIED,
            transport=ctx.transport,
            chat_id=ctx.chat_id,
            message_id=ctx.message_id,
            query_hash=qhash,
            reason="vault sealed",
        )
        raise VaultSealed(
            "Vault is sealed. Run `make unlock` at a terminal; the passphrase cannot "
            "be supplied any other way, and no model can supply it."
        )

    if ctx.transport == CLI:
        return "cli:local", qhash

    # Gate 2: verified identity.
    try:
        principal = identity.verify(ctx.headers)
    except identity.IdentityError as exc:
        audit.record_safe(
            _conn,
            tool=tool,
            decision=audit.DENIED,
            transport=ctx.transport,
            chat_id=ctx.chat_id,
            message_id=ctx.message_id,
            query_hash=qhash,
            reason=f"identity: {exc}",
        )
        raise

    # Gate 3: a human approved this exact request.
    try:
        grants.REGISTRY.request(
            subject=principal.subject,
            principal=str(principal),
            tool=tool,
            chat_id=ctx.chat_id,
            message_id=ctx.message_id,
            qhash=qhash,
            query_preview=payload[:300],
        )
    except grants.PendingApproval as pending:
        audit.record_safe(
            _conn,
            tool=tool,
            decision=audit.PENDING,
            transport=ctx.transport,
            principal=str(principal),
            chat_id=ctx.chat_id,
            message_id=ctx.message_id,
            query_hash=qhash,
            reason=f"awaiting approval code {pending.grant.code}",
        )
        raise

    return str(principal), qhash


def search(
    query: str, *, domains: list[str] | None = None, limit: int = 5, ctx: Context | None = None
) -> list[dict]:
    ctx = ctx or Context()
    resolved = _resolve_domains(domains)
    principal, qhash = _gate(ctx, "search_vault", f"{query}|{','.join(resolved)}|{limit}")

    conn = _conn()
    try:
        dense = store.search_semantic(
            conn, embedder.embed_query(query), domains=resolved, limit=limit
        )
        keyword = store.search_keyword(conn, query, domains=resolved, limit=limit)
        merged: dict[str, dict] = {}
        for rank, hit in enumerate(dense):
            merged[hit["doc_id"] + str(hit["chunk_index"])] = {**hit, "_rr": 1 / (60 + rank)}
        for rank, hit in enumerate(keyword):
            key = hit["doc_id"] + str(hit["chunk_index"])
            if key in merged:
                merged[key]["_rr"] += 1 / (60 + rank)
            else:
                merged[key] = {**hit, "_rr": 1 / (60 + rank)}
        results = sorted(merged.values(), key=lambda h: -h["_rr"])[:limit]
        for hit in results:
            hit.pop("_rr", None)

        audit.record(
            conn,
            tool="search_vault",
            decision=audit.ALLOWED,
            transport=ctx.transport,
            principal=principal,
            chat_id=ctx.chat_id,
            message_id=ctx.message_id,
            query_hash=qhash,
            rows_returned=len(results),
        )
        return results
    finally:
        conn.close()


def query_ledger(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    group_by: str | None = None,
    limit: int = 100,
    ctx: Context | None = None,
    **filters: Any,
) -> dict:
    ctx = ctx or Context()
    unknown = set(filters) - LEDGER_FILTERS
    if unknown:
        raise ValueError(
            f"Unsupported filter(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(LEDGER_FILTERS))}"
        )
    if group_by and group_by not in LEDGER_GROUPS:
        raise ValueError(
            f"Unsupported group_by {group_by!r}. Allowed: {', '.join(sorted(LEDGER_GROUPS))}"
        )

    payload = json.dumps(
        {"start": start_date, "end": end_date, "group": group_by, "filters": filters},
        sort_keys=True,
    )
    principal, qhash = _gate(ctx, "query_ledger", payload)

    where, params = ["1=1"], []
    if start_date:
        where.append("txn_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("txn_date <= ?")
        params.append(end_date)
    for column, value in filters.items():
        # Column names come from the whitelist above, never from input.
        where.append(f"{column} = ?")
        params.append(value)
    clause = " AND ".join(where)

    conn = _conn()
    try:
        if group_by:
            expression = LEDGER_GROUPS[group_by]
            rows = conn.execute(
                f"""SELECT {expression} AS bucket, COUNT(*) n, SUM(amount_cents) total_cents
                    FROM ledger WHERE {clause} GROUP BY bucket ORDER BY bucket LIMIT ?""",
                (*params, limit),
            ).fetchall()
            payload_rows = [dict(r) for r in rows]
        else:
            rows = conn.execute(
                f"SELECT * FROM ledger WHERE {clause} ORDER BY txn_date DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            payload_rows = [dict(r) for r in rows]

        totals = conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) total FROM ledger WHERE {clause}",
            params,
        ).fetchone()
        flagged = conn.execute(
            f"SELECT COUNT(*) n FROM ledger WHERE {clause} AND needs_review=1", params
        ).fetchone()["n"]

        audit.record(
            conn,
            tool="query_ledger",
            decision=audit.ALLOWED,
            transport=ctx.transport,
            principal=principal,
            chat_id=ctx.chat_id,
            message_id=ctx.message_id,
            query_hash=qhash,
            rows_returned=len(payload_rows),
        )
        return {
            "rows": payload_rows,
            "count": totals["n"],
            "total_cents": totals["total"],
            "currency": "USD",
            # Surfaced on every response: an unvalidated VLM reading must never
            # silently inflate a total the caller then trusts.
            "needs_review_count": flagged,
        }
    finally:
        conn.close()


def status() -> dict:
    """Deliberately readable while sealed -- it reveals no vault content."""
    unlocked = AGENT.is_unlocked()
    info: dict[str, Any] = {
        "unlocked": unlocked,
        "seconds_remaining": AGENT.seconds_remaining(),
        "vault_db": str(settings.vault_db_path),
        "exists": settings.vault_db_path.exists(),
        "identity_configured": bool(settings.vault_jwt_secret),
        "allowed_emails": settings.allowed_emails,
        "pending_approvals": len(grants.REGISTRY.pending()),
        "domains": domains_in(Tier.VAULT),
    }
    if unlocked:
        conn = _conn()
        try:
            info["chunks_by_domain"] = store.counts_by_domain(conn)
            info["documents"] = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
            info["ledger_rows"] = conn.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]
            info["needs_review"] = conn.execute(
                "SELECT COUNT(*) n FROM ledger WHERE needs_review=1"
            ).fetchone()["n"]
        finally:
            conn.close()
    return info


__all__ = [
    "CLI",
    "MCP",
    "Context",
    "VaultSealed",
    "audit",
    "blobs",
    "query_ledger",
    "search",
    "status",
    "store",
]
