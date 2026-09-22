"""Vault MCP server.

Runs as its own container so the in-memory key never shares an address space
with the open-tier query surface. Every tool here passes all three gates:
unlocked vault, verified identity, per-request human approval.

This process holds the key, so `make unlock` has to reach *it* specifically.
That happens over a Unix socket on the data volume, never over the network --
an HTTP unlock endpoint would be reachable from every container on llm-net,
including Open WebUI, handing the model a way to try passphrases.
"""

from __future__ import annotations

import logging

from mcp.server.mcpserver import Context, MCPServer

from app.config import settings
from app.domains import Tier, domains_in
from app.vault import control, grants, identity, service
from app.vault.crypto import VaultSealed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = MCPServer("rag-vault")


def _context(ctx: Context | None) -> service.Context:
    """Build the request context from HTTP headers.

    The headers themselves are client-supplied and untrusted; the only one that
    carries weight is the signed JWT, and identity.verify checks its signature
    against a secret shared with Open WebUI. chat_id binds an approval to one
    conversation and message_id is recorded for the audit log, so a forged pair
    can only ever make an approval harder to obtain, never easier.
    """
    headers = dict(ctx.headers or {}) if ctx is not None else {}
    lookup = {k.lower(): v for k, v in headers.items()}
    return service.Context(
        transport=service.MCP,
        headers=headers,
        chat_id=lookup.get("x-openwebui-chat-id", ""),
        message_id=lookup.get("x-openwebui-message-id", ""),
    )


def _denied(exc: Exception, kind: str) -> dict:
    return {"error": kind, "detail": str(exc)}


def _pending(exc: grants.PendingApproval) -> dict:
    """Hand back the approval code AND the exact call it covers.

    The grant is bound to a hash of these arguments, which is the whole point
    -- a human approved one specific query and nothing else. But the hash is
    opaque, so without echoing the arguments the caller has to guess what it
    asked a turn ago. Models reword between turns, and a reworded retry mints
    a fresh PENDING instead of redeeming the approval the owner just gave.
    Observed twice on the first end-to-end run.

    Echoing the arguments loosens nothing: the binding, the single use and the
    chat scope are unchanged, and the owner still reads the real query before
    approving. It only removes the guessing.
    """
    return {
        "error": "PENDING_APPROVAL",
        "code": exc.grant.code,
        "retry_with": exc.grant.arguments,
        "detail": (
            f"Approval required. The account holder must run "
            f"`make approve CODE={exc.grant.code}` at a terminal. "
            "Once they confirm, call this tool AGAIN with exactly the arguments in "
            "`retry_with`, unchanged. The approval is bound to those exact arguments: "
            "rewording the query, or altering any value, creates a NEW request that "
            "needs its own approval rather than redeeming this one. "
            "Do not retry until the owner says they have approved it."
        ),
    }


@mcp.tool()
def search_vault(
    query: str,
    domains: list[str] | None = None,
    limit: int = 5,
    include_superseded: bool = False,
    include_stale: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Search encrypted personal and financial documents.

    Requires an unlocked vault, a verified identity, and a per-request approval
    that a human grants at a terminal. The first call returns an approval code
    rather than data; call again after it is approved.

    Each result carries a `citation`. Quote figures -- amounts, dates, account
    references -- EXACTLY as they appear and cite them; do not restate them in
    your own words. For sums across many records use `query_ledger`, which adds
    integer cents in SQL, rather than adding up numbers you read here.

    Args:
        query: What to look for.
        domains: Restrict to specific vault domains.
        limit: Maximum results (default 5).
        include_superseded: Also return documents a newer version replaced.
        include_stale: Also return documents marked out of date. Their content
            arrives prefixed with a `[STALE ...]` warning; pass it on.
    """
    try:
        rows = service.search(
            query,
            domains=domains,
            limit=limit,
            include_superseded=include_superseded,
            include_stale=include_stale,
            ctx=_context(ctx),
        )
        return {"results": rows, "count": len(rows)}
    except VaultSealed as exc:
        return _denied(exc, "VAULT_SEALED")
    except grants.PendingApproval as exc:
        return _pending(exc)
    except identity.IdentityError as exc:
        return _denied(exc, "IDENTITY_REJECTED")
    except PermissionError as exc:
        return _denied(exc, "NOT_PERMITTED")


@mcp.tool()
def fetch_context(
    doc_id: str, chunk_index: int, before: int = 1, after: int = 1, ctx: Context | None = None
) -> dict:
    """Return the chunks either side of a vault search result, in order.

    This returns vault content, so it passes the same three gates as
    `search_vault` and needs its own approval. An earlier approval for the
    search does not carry over -- otherwise a whole document could be walked
    out one neighbour at a time on the strength of a single grant.

    Args:
        doc_id: From a `search_vault` result.
        chunk_index: From the same result.
        before: Chunks to include before it (default 1).
        after: Chunks to include after it (default 1).
    """
    try:
        rows = service.fetch_context(
            doc_id, chunk_index, before=before, after=after, ctx=_context(ctx)
        )
        return {"results": rows, "count": len(rows)}
    except VaultSealed as exc:
        return _denied(exc, "VAULT_SEALED")
    except grants.PendingApproval as exc:
        return _pending(exc)
    except identity.IdentityError as exc:
        return _denied(exc, "IDENTITY_REJECTED")
    except PermissionError as exc:
        return _denied(exc, "NOT_PERMITTED")


@mcp.tool()
def query_ledger(
    start_date: str | None = None,
    end_date: str | None = None,
    merchant: str | None = None,
    category: str | None = None,
    group_by: str | None = None,
    limit: int = 100,
    ctx: Context | None = None,
) -> dict:
    """Aggregate receipts and transactions with exact arithmetic.

    Totals come from SQL over typed integer cents, not from summarising
    retrieved text, so they are exact rather than approximate.

    Args:
        start_date: ISO date, inclusive (YYYY-MM-DD).
        end_date: ISO date, inclusive.
        merchant: Exact merchant name.
        category: Exact category.
        group_by: One of month, merchant, category.
        limit: Maximum rows (default 100).
    """
    filters = {k: v for k, v in (("merchant", merchant), ("category", category)) if v}
    try:
        return service.query_ledger(
            start_date=start_date,
            end_date=end_date,
            group_by=group_by,
            limit=limit,
            ctx=_context(ctx),
            **filters,
        )
    except VaultSealed as exc:
        return _denied(exc, "VAULT_SEALED")
    except grants.PendingApproval as exc:
        return _pending(exc)
    except identity.IdentityError as exc:
        return _denied(exc, "IDENTITY_REJECTED")
    except ValueError as exc:
        return _denied(exc, "INVALID_ARGUMENT")


@mcp.tool()
def vault_status() -> dict:
    """Whether the vault is unlocked and what it holds. Reveals no contents."""
    return service.status()


def main() -> None:
    if not settings.vault_jwt_secret:
        # Not fatal -- the server still serves vault_status and still denies
        # everything else, which is the safe direction.
        logger.warning(
            "VAULT_JWT_SECRET is unset. No caller's identity can be verified, so every "
            "model-initiated vault call will be denied. Set it to the same value as "
            "FORWARD_USER_INFO_HEADER_JWT_SECRET in the Open WebUI environment."
        )
    if not settings.allowed_emails:
        logger.warning("VAULT_ALLOWED_EMAILS is empty; no identity is permitted.")

    control.start()
    logger.info(
        "Vault MCP server on %s:%d/mcp  domains=%s  (sealed until `make unlock`)",
        settings.mcp_host,
        settings.vault_port,
        ",".join(domains_in(Tier.VAULT)),
    )
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.vault_port)


if __name__ == "__main__":
    main()
