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

from app import documents
from app.config import settings
from app.domains import DOMAIN_TIERS, Tier, UnknownDomainError, domains_in, tier_of
from app.pipeline import qdrant_store, state
from app.sources import Source, SourceConfigError, all_sources, load_sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

mcp = MCPServer("rag-docs")


@mcp.tool()
def search_docs(
    query: str,
    limit: int = 3,
    domains: list[str] | None = None,
    source_id: str | None = None,
    hybrid: bool = True,
    include_superseded: bool = False,
    include_stale: bool = False,
) -> list[dict]:
    """Search indexed documentation by meaning and by exact term.

    Covers the open tier only: manuals, SDK docs, notes, infra plans. Financial
    and personal material is not searchable here.

    Each result carries a `citation` such as `pump.pdf, page 12`. When you use a
    number from a result -- a torque figure, a voltage, a part number, a command
    flag -- QUOTE IT EXACTLY as it appears and give the citation alongside it.
    Do not round it, convert its units, or paraphrase the sentence it sits in. A
    wrong torque spec that reads fluently is worse than an awkward quote, and
    the citation is what lets the reader check it.

    If a result looks like it stops mid-procedure, call `fetch_context` on its
    `doc_id` and `chunk_index` rather than guessing the rest.

    Args:
        query: Natural language question, or an exact term like a part number.
        limit: Maximum results (default 3). Raise it when the first results
            are near misses rather than rephrasing the same query.
        domains: Restrict to these domains. Defaults to every open-tier domain.
        source_id: Restrict to a single configured source.
        hybrid: Fuse semantic and keyword matching (default). Exact identifiers
            rely on the keyword half, so leave this on unless comparing.
        include_superseded: Also return documents a newer version replaced.
            Off by default; such a result carries `lifecycle` and
            `superseded_by`, which names the replacement.
        include_stale: Also return documents marked out of date. Off by default.
            Their content arrives prefixed with a `[STALE ...]` warning, which
            you must pass on rather than strip.
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

    hits = qdrant_store.search(
        query,
        limit=limit,
        domains=selected,
        source_id=source_id,
        hybrid=hybrid,
        include_superseded=include_superseded,
        include_stale=include_stale,
    )
    return [documents.for_model(h) for h in hits]


@mcp.tool()
def fetch_context(doc_id: str, chunk_index: int, before: int = 1, after: int = 1) -> list[dict]:
    """Return the chunks either side of a search result, in order.

    Use this when a result ends mid-procedure or mid-table: the step that
    completes it is usually the next chunk, which did not score highly enough
    to be returned on its own.

    Open tier only. A vault `doc_id` returns an error rather than data -- vault
    content requires the vault server, an unlocked vault, a verified identity
    and a per-request approval.

    Args:
        doc_id: From a `search_docs` result.
        chunk_index: From the same result.
        before: Chunks to include before it (default 1).
        after: Chunks to include after it (default 1).
    """
    # The real guarantee is structural: Qdrant holds no vault points, so a
    # vault doc_id finds nothing here whatever this check does. The check is
    # here to say so out loud rather than return a confusing empty list.
    with state.reader() as conn:
        row = state.get_document(conn, doc_id)
    if row is not None and row["tier"] != Tier.OPEN.value:
        return [
            {
                "error": f"{doc_id} is a vault document and is not readable from this server.",
                "hint": "Use the vault tool server; it needs an unlocked vault and an approval.",
            }
        ]
    rows = qdrant_store.fetch_context(doc_id, chunk_index, before=before, after=after)
    return [documents.for_model(r) for r in rows]


@mcp.tool()
def list_sources() -> list[dict]:
    """List configured sources with their domain, tier and last sync status."""
    try:
        sources = {s.id: s for s in all_sources()}
        disabled = {s.id for s in load_sources() if not s.enabled}
    except SourceConfigError as exc:
        return [{"error": str(exc)}]

    with state.reader() as conn:
        recorded = {r["source_id"]: dict(r) for r in state.all_sources(conn)}

    # This server has no inbox mount, so the inbox sources -- one per
    # directory -- cannot be enumerated here, and they are where most documents
    # live. The worker records every source it syncs, so fill them in from
    # that. A source disabled in sources.yaml stays hidden.
    for source_id, row in recorded.items():
        if source_id in sources or source_id in disabled:
            continue
        try:
            tier_of(row["domain"])
        except UnknownDomainError:
            continue
        sources[source_id] = Source(id=source_id, type=row["source_type"], domain=row["domain"])

    out = []
    for source_id, source in sources.items():
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
        by_lifecycle = state.count_by_lifecycle(conn)
        flagged = state.flagged_pages(conn)
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
        "documents_by_lifecycle": by_lifecycle,
        "last_run": runs[0] if runs else None,
        # Surfaced rather than hidden: an index that silently omits scanned
        # PDFs looks complete when it is not.
        "ocr_required_count": by_status.get(state.OCR_REQUIRED, 0),
        # Pages whose layout could not be decided from whitespace alone. Text
        # from them is in reading order, which is right for prose and wrong for
        # a table, so treat figures quoted from these documents with care.
        "undecided_layout_pages": sum(r["flagged_pages"] for r in flagged),
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
