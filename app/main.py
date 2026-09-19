"""Open-tier MCP server.

Serves manuals, SDK docs, notes and infra plans. Holds no key, links no crypto,
and has no vault mount -- a compromise here cannot reach vault.db.

Vault domains are not served from this process at all. Asking for one returns a
pointer to the vault server rather than data, so the boundary is visible rather
than silently empty.
"""

from __future__ import annotations

import logging

from mcp.server.mcpserver import MCPServer

from app.config import settings
from app.domains import DOMAIN_TIERS, Tier, UnknownDomainError, domains_in, tier_of
from app.pipeline import qdrant_store, state
from app.sources import SourceConfigError, all_sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = MCPServer("rag-docs")


@mcp.tool()
def search_docs(
    query: str,
    limit: int = 5,
    domains: list[str] | None = None,
    source_id: str | None = None,
    hybrid: bool = True,
) -> list[dict]:
    """Search indexed documentation by meaning and by exact term.

    Covers the open tier only: manuals, SDK docs, notes, infra plans. Financial
    and personal material is not searchable here.

    Args:
        query: Natural language question, or an exact term like a part number.
        limit: Maximum results (default 5).
        domains: Restrict to these domains. Defaults to every open-tier domain.
        source_id: Restrict to a single configured source.
        hybrid: Fuse semantic and keyword matching (default). Exact identifiers
            rely on the keyword half, so leave this on unless comparing.
    """
    open_domains = domains_in(Tier.OPEN)
    if domains:
        rejected = []
        for domain in domains:
            try:
                if tier_of(domain) is Tier.VAULT:
                    rejected.append(domain)
            except UnknownDomainError:
                return [
                    {
                        "error": f"Unknown domain {domain!r}.",
                        "valid_domains": open_domains,
                    }
                ]
        if rejected:
            return [
                {
                    "error": (
                        f"{', '.join(rejected)} is stored in the encrypted vault and is not "
                        "searchable from this server."
                    ),
                    "hint": (
                        "Vault data requires the vault tool server, an unlocked vault, a "
                        "verified identity and a per-request approval."
                    ),
                }
            ]
        selected = domains
    else:
        selected = open_domains

    return qdrant_store.search(
        query, limit=limit, domains=selected, source_id=source_id, hybrid=hybrid
    )


@mcp.tool()
def list_sources() -> list[dict]:
    """List configured sources with their domain, tier and last sync status."""
    try:
        configured = {s.id: s for s in all_sources()}
    except SourceConfigError as exc:
        return [{"error": str(exc)}]

    with state.reader() as conn:
        recorded = {r["source_id"]: dict(r) for r in state.all_sources(conn)}

    out = []
    for source_id, source in configured.items():
        row = recorded.get(source_id, {})
        entry = {
            "source_id": source_id,
            "type": source.type,
            "domain": source.domain,
            "tier": source.tier.value,
            "cadence": source.cadence,
            "last_status": row.get("last_status"),
            "last_finished_at": row.get("last_finished_at"),
            "last_error": row.get("last_error"),
            "docs_seen": row.get("docs_seen"),
        }
        if source.tier is Tier.OPEN:
            entry["chunks"] = qdrant_store.count_points(
                qdrant_store.build_filter(source_id=source_id)
            )
        else:
            entry["chunks"] = "encrypted (query the vault server)"
        out.append(entry)
    return out


@mcp.tool()
def get_index_status() -> dict:
    """Index health: totals per domain, last run, and known gaps."""
    with state.reader() as conn:
        by_status = state.count_by_status(conn)
        runs = [dict(r) for r in state.last_runs(conn, 1)]
        quarantined = len(state.open_quarantine(conn))

    return {
        "collection": settings.collection_name,
        "embed_model": settings.embed_model,
        "embed_dim": settings.embed_dim,
        "total_points": qdrant_store.count_points(),
        "by_domain": {
            d: qdrant_store.count_points(qdrant_store.build_filter(domains=[d]))
            for d in domains_in(Tier.OPEN)
        },
        "documents_by_status": by_status,
        "last_run": runs[0] if runs else None,
        # Surfaced rather than hidden: an index that silently omits scanned
        # PDFs looks complete when it is not.
        "ocr_required_count": by_status.get(state.OCR_REQUIRED, 0),
        "quarantined_count": quarantined,
        "vault_domains": domains_in(Tier.VAULT),
        "note": "Vault domains are served by the separate vault tool server.",
    }


def main() -> None:
    logger.info("Initialising Qdrant collection...")
    qdrant_store.init_collection()
    # Deliberately NOT state.init(): this server is a reader and mounts
    # data/state read-only. The ingestion worker owns that file.
    logger.info(
        "Open-tier MCP server on %s:%d/mcp  domains=%s",
        settings.mcp_host,
        settings.mcp_port,
        ",".join(domains_in(Tier.OPEN)),
    )
    mcp.run(transport="streamable-http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
