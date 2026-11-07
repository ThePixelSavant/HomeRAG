"""The admin report's data: plain dicts built from rag.db and Qdrant (ADR-031).

Read-only and OPEN TIER ONLY, enforced here rather than trusted to the caller.
rag.db holds only open-tier documents today, but its `sources` table also
records vault inbox sources (`inbox:receipts`), and a run's notes carry every
failing source's error text. So:

- documents are filtered by `tier='open'` and an open domain,
- sources and quarantine rows by open domain,
- a run is shown only through the open sources it touched, and its notes are
  never shown -- an open source's own `last_error` is.

Nothing here imports `app.vault`. Every function takes a reader connection;
nothing writes.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date

from app import documents
from app.config import settings
from app.domains import Tier, domains_in
from app.pipeline import state

logger = logging.getLogger(__name__)

VIEWS = ("overview", "documents", "lifecycle", "quarantine", "eval")


def _open_domains() -> list[str]:
    return domains_in(Tier.OPEN)


def _open_docs_where(alias: str = "") -> tuple[str, list]:
    """The clause every documents query starts from."""
    od = _open_domains()
    p = f"{alias}." if alias else ""
    return (
        f"{p}tier=? AND {p}domain IN ({','.join('?' for _ in od)})",
        [Tier.OPEN.value, *od],
    )


def _group(conn: sqlite3.Connection, column: str) -> dict[str, int]:
    where, params = _open_docs_where()
    try:
        rows = conn.execute(
            f"SELECT {column} k, COUNT(*) n FROM documents WHERE {where} GROUP BY {column}", params
        ).fetchall()
    except sqlite3.OperationalError:
        # A database predating the column; readers cannot migrate.
        return {}
    return {r["k"]: r["n"] for r in rows}


def _open_sources(conn: sqlite3.Connection) -> list[dict]:
    od = set(_open_domains())
    return [dict(r) for r in state.all_sources(conn) if r["domain"] in od]


def _qdrant_counts() -> dict:
    # Imported here so the module (and its tests) do not need a live Qdrant to
    # load, and a Qdrant outage degrades one tile instead of the whole page.
    from app.pipeline import qdrant_store

    try:
        return {
            "total": qdrant_store.count_points(
                qdrant_store.build_filter(domains=_open_domains())
            ),
            "by_domain": {
                d: qdrant_store.count_points(qdrant_store.build_filter(domains=[d]))
                for d in _open_domains()
            },
        }
    except Exception as exc:  # noqa: BLE001 -- shown on the page, not raised
        logger.warning("qdrant count failed: %s", exc)
        return {"error": str(exc)}


def overview(conn: sqlite3.Connection) -> dict:
    sources = _open_sources(conn)
    open_ids = {s["source_id"] for s in sources}

    runs = []
    for row in state.last_runs(conn, 20):
        touched = [s for s in (row["sources_run"] or "").split(",") if s in open_ids]
        if not touched:
            continue
        runs.append({
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "sources": touched,
        })
        if len(runs) == 10:
            break

    # Not state.transcription_counts / rejected_transcriptions: those do not
    # filter by tier, and this module promises open tier only.
    where, params = _open_docs_where("d")
    join = "FROM page_transcriptions t JOIN documents d USING (doc_id)"
    try:
        transcribed = {
            r["status"]: r["n"]
            for r in conn.execute(
                f"SELECT t.status, COUNT(*) n {join} WHERE {where} GROUP BY t.status", params
            )
        }
        rejected = [
            dict(r)
            for r in conn.execute(
                f"SELECT d.uri, t.page, t.detail {join} WHERE {where} AND t.status='rejected' "
                "ORDER BY d.uri, t.page LIMIT 10",
                params,
            )
        ]
    except sqlite3.OperationalError:
        transcribed, rejected = {}, []

    by_lifecycle = _group(conn, "lifecycle")
    return {
        "collection": settings.collection_name,
        "embed_model": settings.embed_model,
        "embed_dim": settings.embed_dim,
        "points": _qdrant_counts(),
        "documents_by_status": _group(conn, "status"),
        "documents_by_lifecycle": by_lifecycle,
        "not_active": sum(n for k, n in by_lifecycle.items() if k != documents.ACTIVE),
        "quarantined": len(quarantine(conn)["items"]),
        "transcribed_pages": transcribed,
        "rejected_transcriptions": rejected,
        "sources": sources,
        "runs": runs,
        "latest_eval": (eval_history(conn, limit=1) or [None])[-1],
    }


def document_list(
    conn: sqlite3.Connection,
    *,
    domain: str | None = None,
    match: str | None = None,
    include_all: bool = False,
) -> dict:
    """The same inventory as `rag list`, open tier only."""
    where, params = _open_docs_where()
    clauses = [where]
    if domain:
        clauses.append("domain=?")
        params.append(domain)
    if match:
        clauses.append("uri LIKE ? ESCAPE '\\'")
        escaped = match.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    if not include_all:
        clauses.append("lifecycle=?")
        params.append(documents.ACTIVE)
    rows = conn.execute(
        "SELECT uri, domain, lifecycle, chunk_count, status, flagged_pages, indexed_at, "
        f"size_bytes FROM documents WHERE {' AND '.join(clauses)} ORDER BY domain, uri",
        params,
    ).fetchall()
    items = [dict(r) for r in rows]
    return {
        "filters": {"domain": domain, "match": match, "all": include_all},
        "domains": _open_domains(),
        "items": items,
        "total_chunks": sum(r["chunk_count"] for r in items),
    }


def lifecycle(conn: sqlite3.Connection, today: str | None = None) -> dict:
    """What `rag lifecycle` shows, plus which review dates have passed."""
    today = today or date.today().isoformat()
    where, params = _open_docs_where()
    rows = conn.execute(
        "SELECT uri, domain, lifecycle, lifecycle_reason, lifecycle_set_at, superseded_by, "
        f"review_after FROM documents WHERE {where} "
        "AND (lifecycle<>? OR review_after IS NOT NULL) ORDER BY lifecycle, uri",
        [*params, documents.ACTIVE],
    ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["review_due"] = bool(
            r["lifecycle"] == documents.ACTIVE and r["review_after"] and r["review_after"] <= today
        )
        items.append(item)
    return {"counts": _group(conn, "lifecycle"), "items": items, "today": today}


def quarantine(conn: sqlite3.Connection) -> dict:
    """Open quarantine entries: rule NAMES only.

    `reason` carries each finding's detail. It is redacted at the source, but
    the rule names are all a reviewer needs to know where to look, and the
    page is the wrong place to second-guess that redaction.
    """
    od = set(_open_domains())
    items = []
    for r in state.open_quarantine(conn):
        if r["domain"] not in od:
            continue
        items.append({
            "id": r["id"],
            "path": r["origin_path"],
            "domain": r["domain"],
            "rules": [x for x in (r["rules"] or "").split(",") if x],
            "detected_at": r["detected_at"],
        })
    return {"items": items}


def eval_history(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return state.eval_history(conn, limit=limit)


def eval_view(conn: sqlite3.Connection) -> dict:
    runs = eval_history(conn)
    latest = runs[-1] if runs else None
    previous = runs[-2] if len(runs) > 1 else None
    comparison = []
    if latest:
        before = {q["id"]: q["rank"] for q in previous["per_question"]} if previous else {}
        same_set = bool(previous and previous["questions_hash"] == latest["questions_hash"])
        for q in latest["per_question"]:
            comparison.append({
                "id": q["id"],
                "rank": q["rank"],
                "previous_rank": before.get(q["id"]) if previous else None,
                "top3_chars": q["top3_chars"],
            })
        return {"runs": runs, "latest": latest, "comparison": comparison,
                "same_question_set": same_set}
    return {"runs": [], "latest": None, "comparison": [], "same_question_set": False}


def build(view: str, conn: sqlite3.Connection, params: dict | None = None) -> dict:
    """Dispatch by view name. Unknown views raise ValueError."""
    params = params or {}
    if view == "overview":
        return overview(conn)
    if view == "documents":
        domain = params.get("domain") or None
        if domain and domain not in _open_domains():
            # Includes vault domains: asking for one is answered the same way
            # as a typo, not with an empty list that implies it was searched.
            raise ValueError(f"Unknown open-tier domain {domain!r}.")
        return document_list(
            conn, domain=domain, match=params.get("match") or None,
            include_all=bool(params.get("all")),
        )
    if view == "lifecycle":
        return lifecycle(conn)
    if view == "quarantine":
        return quarantine(conn)
    if view == "eval":
        return eval_view(conn)
    raise ValueError(f"Unknown view {view!r}. Valid: {', '.join(VIEWS)}.")
