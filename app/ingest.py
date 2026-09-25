"""Control-plane CLI.

argparse rather than typer: a handful of subcommands does not justify another
dependency in an image that will carry Playwright in Phase 2.

Querying the open tier here goes straight to Qdrant rather than through MCP, so
it still works when the servers are down.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import shutil
import sys
from pathlib import Path

import yaml

from app import documents
from app.config import settings
from app.domains import DOMAIN_TIERS, Tier, UnknownDomainError, domains_in, tier_of
from app.pipeline import embedder, qdrant_store, state, sync
from app.sources import Source, SourceConfigError, all_sources
from app.vault.crypto import WrongPassphrase
from app.vault.keyagent import AGENT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest")

OK, WARN, BAD = "ok", "warn", "FAIL"


def _snippet(text: str, width: int = 260) -> str:
    """One-line preview: chunks carry newlines and a heading breadcrumb."""
    flat = " ".join(text.split())
    return flat[:width] + ("..." if len(flat) > width else "")


def _print_rows(rows: list[tuple[str, str, str]]) -> None:
    for status, label, detail in rows:
        marker = {OK: "  ok  ", WARN: " warn ", BAD: " FAIL "}[status]
        print(f"[{marker}] {label:<26} {detail}")


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def cmd_doctor(args) -> int:
    import platform

    rows: list[tuple[str, str, str]] = []
    failed = False

    rows.append((OK, "architecture", f"{platform.machine()} / python {platform.python_version()}"))

    try:
        client = qdrant_store.get_client()
        version = client.info().version if hasattr(client, "info") else "?"
        rows.append((OK, "qdrant", f"reachable at {settings.qdrant_url} (v{version})"))
        try:
            qdrant_store.verify_fingerprint()
            rows.append((OK, "embedding fingerprint", f"{settings.embed_model} / {settings.embed_dim}d"))
        except qdrant_store.FingerprintMismatch as exc:
            rows.append((BAD, "embedding fingerprint", str(exc)[:96]))
            failed = True
    except Exception as exc:  # noqa: BLE001
        rows.append((BAD, "qdrant", f"unreachable: {type(exc).__name__}: {exc}"))
        failed = True

    if shutil.which("pdftotext"):
        rows.append((OK, "pdftotext", shutil.which("pdftotext")))
    else:
        rows.append((WARN, "pdftotext", "missing; PDFs will fail (install poppler-utils)"))

    try:
        embedder.count_tokens("warm")
        rows.append((OK, "model cache", str(settings.fastembed_cache_path)))
    except Exception as exc:  # noqa: BLE001
        rows.append((BAD, "model cache", f"{type(exc).__name__}: {exc}"))
        failed = True

    try:
        state.init()
        rows.append((OK, "state db", str(settings.state_db_path)))
    except Exception as exc:  # noqa: BLE001
        rows.append((BAD, "state db", f"not writable: {exc}"))
        failed = True

    # Every configured path must exist and be non-empty. This is the check that
    # catches the failure mode the sweep guards against: a source pointing at
    # nothing looks identical to a source whose files were all deleted.
    try:
        sources = all_sources()
    except SourceConfigError as exc:
        rows.append((BAD, "sources.yaml", str(exc)[:96]))
        _print_rows(rows)
        return 1

    for source in sources:
        if source.path is None:
            continue
        if not source.path.exists():
            rows.append((BAD, f"source {source.id}", f"{source.path} does not exist"))
            failed = True
        elif not any(source.path.rglob("*")):
            rows.append((WARN, f"source {source.id}", f"{source.path} is empty"))
        else:
            files, complete = sync.enumerate_files(source)
            rows.append(
                (OK if complete else BAD, f"source {source.id}",
                 f"{len(files)} file(s), {source.domain} -> {source.tier.value}")
            )
            failed = failed or not complete

    try:
        from app.vault import control

        reply = control.send({"action": "status"})
        status = reply.get("status", {})
        rows.append(
            (OK, "vault", f"{'unlocked' if status.get('unlocked') else 'sealed'}; "
                          f"identity {'configured' if status.get('identity_configured') else 'NOT configured'}")
        )
        if not status.get("identity_configured"):
            rows.append((WARN, "VAULT_JWT_SECRET", "unset; every model-initiated vault call will be denied"))
    except control.ControlForbidden:
        # Expected: this container is unprivileged and the socket belongs to
        # the vault server's user. Not a warning -- nothing is wrong.
        rows.append((OK, "vault", "not readable from here (by design); use `make vault-status`"))
    except Exception as exc:  # noqa: BLE001
        rows.append((WARN, "vault", f"control socket unavailable: {exc}"))

    with state.reader() as conn:
        counts = state.count_by_status(conn)
        pending = len(state.open_quarantine(conn))
    if counts.get(state.OCR_REQUIRED):
        rows.append((WARN, "ocr backlog", f"{counts[state.OCR_REQUIRED]} document(s) have no text layer"))
    if pending:
        rows.append((WARN, "quarantine", f"{pending} document(s) awaiting review (make review-quarantine)"))

    _print_rows(rows)
    return 1 if failed else 0


# --------------------------------------------------------------------------
# ingest / reindex
# --------------------------------------------------------------------------


def _select(args) -> list[Source]:
    sources = all_sources()
    if getattr(args, "source", None):
        sources = [s for s in sources if s.id == args.source]
        if not sources:
            raise SystemExit(f"No source with id {args.source!r}")
    if getattr(args, "domain", None):
        sources = [s for s in sources if s.domain == args.domain]
    return sources


def _unlock_for_vault_sources(sources: list[Source]) -> None:
    """Derive the vault key in this process, from a passphrase typed here.

    The worker runs as its own container, so `make unlock` -- which reaches the
    vault *server* over the control socket -- leaves this process's key agent
    empty. Without this, a vault-tier source queues as sealed no matter what
    the server knows.

    The passphrase is prompted for rather than fetched, on purpose. The key
    never travels over the socket, so nothing that can open that socket gains
    the ability to decrypt the vault, and the rule that holds the whole design
    up still holds: a passphrase is typed at a terminal, and a model has no
    terminal.

    The cost is that vault-tier sources cannot be ingested by a scheduled run.
    They queue, and the next interactive `make ingest` picks them up. Open-tier
    sources are unaffected and still run unattended.
    """
    if not any(s.is_vault for s in sources):
        return
    if AGENT.is_unlocked():
        return
    if not sys.stdin.isatty():
        logger.info(
            "Vault-tier sources present but no terminal to unlock with; they will queue. "
            "Run `make ingest` interactively to take them."
        )
        return

    vault_ids = ", ".join(s.id for s in sources if s.is_vault)
    print(f"Vault-tier sources in this run: {vault_ids}")
    passphrase = getpass.getpass("Vault passphrase (blank to skip and queue them): ")
    if not passphrase:
        print("Skipped. Vault sources will queue.")
        return
    try:
        AGENT.unlock(passphrase)
    except WrongPassphrase as exc:
        raise SystemExit(str(exc)) from None


def _run(sources: list[Source], *, force: bool, dry_run: bool) -> int:
    if not dry_run:
        _unlock_for_vault_sources(sources)

    run_id = state.new_run_id()
    qdrant_store.init_collection()

    with sync.sync_lock():
        with state.writer() as conn:
            state.start_run(conn, run_id, [s.id for s in sources])

        results = []
        for source in sources:
            logger.info("Syncing %s (%s -> %s)", source.id, source.domain, source.tier.value)
            results.append(sync.sync_source(source, run_id, force=force, dry_run=dry_run))

        errors = [r for r in results if r.error]
        with state.writer() as conn:
            state.finish_run(conn, run_id, "error" if errors else "ok",
                             "; ".join(r.error for r in errors if r.error))

    print(f"\nrun {run_id}")
    for result in results:
        print(
            f"  {result.source_id:22s} seen={result.docs_seen:<4} indexed={result.docs_indexed:<4} "
            f"skipped={result.docs_skipped:<4} chunks+={result.chunks_upserted:<5} "
            f"chunks-={result.chunks_deleted:<4}"
        )
        for label, count in (("quarantined", result.quarantined), ("ocr_required", result.ocr_required),
                             ("queued(sealed)", result.queued_sealed),
                             ("skipped(retracted)", result.skipped_retracted),
                             ("empty (nothing to index)", result.docs_empty),
                             ("pages flagged for review", result.flagged_pages),
                             ("failed", result.failed)):
            if count:
                print(f"      {label}: {count}")
        if result.error:
            print(f"      ERROR: {result.error}")
        for note in result.notes[:5]:
            print(f"      note: {note}")
    return 1 if errors else 0


def cmd_ingest(args) -> int:
    return _run(_select(args), force=args.force, dry_run=args.dry_run)


def cmd_reindex(args) -> int:
    sources = [s for s in all_sources() if s.id == args.source_id]
    if not sources:
        raise SystemExit(f"No source with id {args.source_id!r}")
    source = sources[0]
    if not source.is_vault:
        # Blank the stored hash so every document reads as changed, rather
        # than deleting the rows. Deleting also dropped the tombstone that
        # keeps a retracted document out -- so reindex re-indexed it -- and
        # every other document's lifecycle, which re-indexing must preserve.
        placeholders = ",".join("?" for _ in documents.TOMBSTONED)
        with state.writer() as conn:
            conn.execute(
                "UPDATE documents SET content_hash='' "
                f"WHERE source_id=? AND lifecycle NOT IN ({placeholders})",
                (source.id, *sorted(documents.TOMBSTONED)),
            )
    return _run(sources, force=True, dry_run=False)


# --------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------


def cmd_add(args) -> int:
    source_file = Path(args.path)
    if not source_file.is_file():
        raise SystemExit(f"{source_file} is not a file")
    try:
        tier = tier_of(args.domain)
    except UnknownDomainError as exc:
        raise SystemExit(str(exc)) from None

    target_dir = settings.inbox_path / args.domain
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source_file.name

    if args.move:
        shutil.move(str(source_file), target)
    else:
        shutil.copy2(source_file, target)
    print(f"{'Moved' if args.move else 'Copied'} to {target}  (domain={args.domain}, tier={tier.value})")
    if tier is Tier.VAULT:
        print("This is a vault domain: ingestion needs an unlocked vault, and the original "
              "in the inbox will be encrypted into the vault and removed.")
    return _run([s for s in all_sources() if s.id == f"inbox:{args.domain}"],
                force=False, dry_run=False)


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def _resolve_one(conn, uri: str, domain: str | None):
    """Exactly one open-tier document, or a usable error."""
    rows = state.find_by_uri(conn, uri, domain)
    if not rows:
        raise SystemExit(
            f"No open-tier document matches {uri!r}"
            + (f" in domain {domain!r}" if domain else "")
            + ".\nIf it is a vault document, use `make vault-retract` / `make vault-stale`."
        )
    if len(rows) > 1:
        listed = "\n".join(f"  {r['uri']}  ({r['domain']})" for r in rows)
        raise SystemExit(f"{uri!r} matches {len(rows)} documents. Narrow it with DOMAIN=\n{listed}")
    return rows[0]


def _apply(conn, row, lifecycle: str, **fields) -> None:
    """Write a lifecycle change to the state row AND the index, together."""
    state.set_lifecycle(conn, row["doc_id"], lifecycle, **fields)
    if lifecycle == documents.RETRACTED:
        # Deleted, not filtered. "Never returned" then holds because the points
        # are gone, rather than because every query path remembered to exclude
        # them. The state row stays behind as the tombstone.
        qdrant_store.delete_doc(row["doc_id"])
        conn.execute("UPDATE documents SET chunk_count=0 WHERE doc_id=?", (row["doc_id"],))
    else:
        qdrant_store.set_lifecycle(
            row["doc_id"],
            {
                "lifecycle": lifecycle,
                "lifecycle_reason": fields.get("reason"),
                "lifecycle_set_at": state.utcnow(),
                "superseded_by": fields.get("superseded_by"),
            },
        )


def cmd_supersede(args) -> int:
    with state.writer() as conn:
        old = _resolve_one(conn, args.old, args.domain)
        new = _resolve_one(conn, args.new, args.domain)
        if old["doc_id"] == new["doc_id"]:
            raise SystemExit("OLD and NEW are the same document.")
        _apply(
            conn, old, documents.SUPERSEDED,
            reason=f"replaced by {new['uri']}", superseded_by=new["uri"],
        )
        state.set_lifecycle(conn, new["doc_id"], new["lifecycle"], supersedes=old["uri"])
    print(f"{old['uri']}  ->  superseded by  {new['uri']}")
    print("It is excluded from search by default; pass include_superseded to see it.")
    return 0


def cmd_stale(args) -> int:
    with state.writer() as conn:
        row = _resolve_one(conn, args.uri, args.domain)
        if args.after:
            # Scheduled, not applied. The flip happens on the ingest run that
            # first sees the date has passed, so it is recorded and visible.
            state.schedule_review(conn, row["doc_id"], args.after)
            print(f"{row['uri']}: will be marked stale on or after {args.after}")
            return 0
        _apply(conn, row, documents.STALE, reason=args.reason)
    print(f"{row['uri']}: marked stale. Excluded from search unless include_stale is passed.")
    return 0


def cmd_retract(args) -> int:
    with state.writer() as conn:
        row = _resolve_one(conn, args.uri, args.domain)
        _apply(conn, row, documents.RETRACTED, reason=args.reason)
    print(f"{row['uri']}: retracted. {row['chunk_count']} chunk(s) deleted from the index.")
    print("A tombstone keeps it out of future runs even though the file is still on disk.")
    print(f"To undo: make restore URI={row['uri']} && make ingest FORCE=1")
    return 0


def cmd_restore(args) -> int:
    with state.writer() as conn:
        row = _resolve_one(conn, args.uri, args.domain)
        if row["lifecycle"] == documents.ACTIVE:
            print(f"{row['uri']} is already active.")
            return 0
        was = row["lifecycle"]
        state.set_lifecycle(conn, row["doc_id"], documents.ACTIVE, reason=None)
        state.clear_review(conn, row["doc_id"])
        if was == documents.RETRACTED:
            # The chunks were deleted, so clearing the flag is only half of it:
            # nothing comes back until the file is read again.
            conn.execute("UPDATE documents SET content_hash='' WHERE doc_id=?", (row["doc_id"],))
        else:
            qdrant_store.set_lifecycle(
                row["doc_id"],
                {"lifecycle": documents.ACTIVE, "lifecycle_reason": None,
                 "lifecycle_set_at": state.utcnow(), "superseded_by": None},
            )
    print(f"{row['uri']}: restored to active (was {was}).")
    if was == documents.RETRACTED:
        print("Its chunks were deleted. Run `make ingest` to re-index it.")
    return 0


def cmd_list(args) -> int:
    """Every open-tier document, which is the inventory `status` only counts.

    Open tier only, and it says so: vault documents live in the encrypted
    database and are not readable from here at all. `make vault-list` is their
    equivalent and needs an unlocked vault.
    """
    sql = (
        "SELECT uri, domain, lifecycle, chunk_count, status, flagged_pages, "
        "indexed_at, size_bytes FROM documents"
    )
    where, params = [], []
    if args.domain:
        where.append("domain=?")
        params.append(args.domain)
    if args.match:
        where.append("uri LIKE ?")
        params.append(f"%{args.match}%")
    if not args.all:
        where.append("lifecycle=?")
        params.append(documents.ACTIVE)
    if where:
        sql += " WHERE " + " AND ".join(where)

    with state.reader() as conn:
        rows = conn.execute(sql + " ORDER BY domain, uri", params).fetchall()

    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return 0
    if not rows:
        print("No documents match." if (args.domain or args.match) else "Nothing indexed yet.")
        return 0

    width = min(max(len(r["uri"]) for r in rows), 62)
    for row in rows:
        uri = row["uri"] if len(row["uri"]) <= width else "…" + row["uri"][-(width - 1):]
        # Only annotate what is not the ordinary case, so the exceptions are
        # what catches the eye rather than a column of "active" on every line.
        notes = []
        if row["lifecycle"] != documents.ACTIVE:
            notes.append(row["lifecycle"].upper())
        if row["status"] != state.INDEXED:
            notes.append(row["status"])
        if row["flagged_pages"]:
            notes.append(f"{row['flagged_pages']} page(s) flagged")
        suffix = f"  [{', '.join(notes)}]" if notes else ""
        print(f"  {uri:<{width}}  {row['domain']:<10} {row['chunk_count']:>4} chunks"
              f"  {(row['indexed_at'] or '')[:10]}{suffix}")

    total = sum(r["chunk_count"] for r in rows)
    scope = "" if args.all else " active"
    print(f"\n  {len(rows)}{scope} document(s), {total} chunks."
          + ("" if args.all else "  Add --all to include superseded/stale/retracted."))
    return 0


def cmd_lifecycle(args) -> int:
    with state.writer() as conn:
        rows = conn.execute(
            "SELECT uri, domain, lifecycle, lifecycle_reason, lifecycle_set_at, "
            "superseded_by, review_after FROM documents "
            "WHERE lifecycle<>? OR review_after IS NOT NULL ORDER BY lifecycle, uri",
            (documents.ACTIVE,),
        ).fetchall()
        counts = state.count_by_lifecycle(conn)
    if args.json:
        print(json.dumps({"counts": counts, "flagged": [dict(r) for r in rows]},
                         indent=2, default=str))
        return 0
    print("  ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no documents")
    for row in rows:
        detail = row["lifecycle_reason"] or ""
        if row["lifecycle"] == documents.SUPERSEDED and row["superseded_by"]:
            detail = f"-> {row['superseded_by']}"
        elif row["lifecycle"] == documents.ACTIVE and row["review_after"]:
            detail = f"review on {row['review_after']}"
        print(f"  {row['lifecycle']:11s} {row['uri']:48s} ({row['domain']})  {detail}")
    return 0


# --------------------------------------------------------------------------
# query / status
# --------------------------------------------------------------------------


def cmd_query(args) -> int:
    domains = args.domains.split(",") if args.domains else None
    if domains:
        vault = [d for d in domains if tier_of(d) is Tier.VAULT]
        if vault:
            raise SystemExit(
                f"{', '.join(vault)} live in the vault. Use `make vault-query` instead."
            )
    hits = qdrant_store.search(
        args.query, limit=args.limit, domains=domains or domains_in(Tier.OPEN),
        hybrid=not args.dense,
        include_superseded=args.include_superseded,
        include_stale=args.include_stale,
    )
    if args.json:
        print(json.dumps(hits, indent=2, default=str))
        return 0
    if not hits:
        print("no results")
        return 0
    for hit in hits:
        crumb = " > ".join(hit["heading_path"]) if hit["heading_path"] else ""
        flag = "" if hit["lifecycle"] == documents.ACTIVE else f"  [{hit['lifecycle'].upper()}]"
        print(f"\n[{hit['score']:.4f}] {hit['title']}  ({hit['domain']}){flag}")
        print(f"  {hit['citation'] or hit['uri']}  chunk {hit['chunk_index']}"
              + (f"  |  {crumb}" if crumb else ""))
        print(f"  {_snippet(hit['content'])}")
    return 0


_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def _squash(text: str) -> str:
    """Compare text regardless of spacing, case and curly quotes.

    Extraction changes move line breaks and spaces (`1–16,OFF` vs `1–16, OFF`)
    without changing what a chunk says, and must not read as a miss.
    """
    return "".join(text.translate(_QUOTES).lower().split())


def cmd_eval(args) -> int:
    """Rank each question's known answer in the open-tier search.

    Measures what a model gets: `search_docs` returns the top 3 by default, so
    hit@3 is the number that matters, and the size of those 3 is what gets
    prefilled. Ranks are searched to `--depth` so a near miss shows as one.
    """
    questions = yaml.safe_load(Path(args.file).read_text())["questions"]
    rows = []
    for q in questions:
        hits = qdrant_store.search(q["question"], limit=args.depth, domains=domains_in(Tier.OPEN))
        want = _squash(q["expect"])
        rank = next(
            (n for n, h in enumerate(hits, 1) if h["uri"] == q["uri"] and want in _squash(h["content"])),
            None,
        )
        rows.append({"id": q["id"], "rank": rank, "top3_chars": sum(len(h["content"]) for h in hits[:3])})

    total = len(rows)
    summary = {
        "questions": total,
        "hit@1": sum(1 for r in rows if r["rank"] == 1) / total,
        "hit@3": sum(1 for r in rows if r["rank"] and r["rank"] <= 3) / total,
        f"mrr@{args.depth}": sum(1 / r["rank"] for r in rows if r["rank"]) / total,
        "mean_top3_chars": round(sum(r["top3_chars"] for r in rows) / total),
    }
    if args.json:
        print(json.dumps({"summary": summary, "questions": rows}, indent=2))
        return 0

    width = max(len(r["id"]) for r in rows)
    for r in rows:
        rank = str(r["rank"]) if r["rank"] else f">{args.depth}"
        print(f"  {r['id']:<{width}}  rank {rank:>4}  top-3 {r['top3_chars']:>5} chars")
    print(
        f"\nhit@1 {summary['hit@1']:.0%}   hit@3 {summary['hit@3']:.0%}   "
        f"mrr@{args.depth} {summary[f'mrr@{args.depth}']:.3f}   "
        f"mean top-3 {summary['mean_top3_chars']} chars   ({total} questions)"
    )
    return 0


def cmd_status(args) -> int:
    payload: dict = {
        "collection": settings.collection_name,
        "embed_model": settings.embed_model,
        "embed_dim": settings.embed_dim,
    }
    try:
        qdrant_store.init_collection()
        payload["open_tier_points"] = qdrant_store.count_points()
        payload["by_domain"] = {
            d: qdrant_store.count_points(qdrant_store.build_filter(domains=[d]))
            for d in domains_in(Tier.OPEN)
        }
    except Exception as exc:  # noqa: BLE001
        payload["qdrant_error"] = str(exc)

    with state.reader() as conn:
        payload["documents_by_status"] = state.count_by_status(conn)
        payload["documents_by_lifecycle"] = state.count_by_lifecycle(conn)
        payload["flagged_pages"] = [dict(r) for r in state.flagged_pages(conn)]
        payload["quarantined_open"] = len(state.open_quarantine(conn))
        payload["sources"] = [dict(r) for r in state.all_sources(conn)]
        payload["last_runs"] = [dict(r) for r in state.last_runs(conn, 5)]

    try:
        from app.vault import control

        payload["vault"] = control.send({"action": "status"}).get("status")
    except control.ControlForbidden:
        payload["vault"] = {"unreadable_here": "run `make vault-status`"}
    except Exception as exc:  # noqa: BLE001
        payload["vault"] = {"error": str(exc)}

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(f"collection {payload['collection']}  model {payload['embed_model']} "
          f"({payload['embed_dim']}d)")
    print(f"open-tier points: {payload.get('open_tier_points', '?')}")
    for domain, count in (payload.get("by_domain") or {}).items():
        print(f"    {domain:14s} {count}")
    print(f"documents by status: {payload['documents_by_status']}")
    lifecycle = payload["documents_by_lifecycle"]
    print(f"documents by lifecycle: {lifecycle or 'not yet migrated -- run make ingest'}")
    inactive = sum(n for k, n in lifecycle.items() if k != documents.ACTIVE)
    if inactive:
        print(f"  {inactive} not active -- see `make lifecycle`")
    flagged = payload["flagged_pages"]
    if flagged:
        total = sum(r["flagged_pages"] for r in flagged)
        print(f"pages flagged for review: {total} across {len(flagged)} document(s)")
        for row in flagged[:5]:
            print(f"    {row['flagged_pages']:3d}  {row['uri']}  ({row['domain']})")
    if payload["quarantined_open"]:
        print(f"  !! {payload['quarantined_open']} quarantined document(s) awaiting review")
    vault = payload.get("vault") or {}
    if "unreadable_here" in vault:
        print(f"vault: {vault['unreadable_here']}  (its control socket is restricted to "
              "the vault container, which is the point)")
    elif "error" in vault:
        print(f"vault: unavailable ({vault['error']})")
    else:
        print(f"vault: {'unlocked' if vault.get('unlocked') else 'SEALED'}  "
              f"docs={vault.get('documents','?')} chunks={vault.get('chunks_by_domain','?')} "
              f"ledger={vault.get('ledger_rows','?')} needs_review={vault.get('needs_review','?')}")
    print("\nsources:")
    for row in payload["sources"]:
        print(f"  {row['source_id']:22s} {row['last_status'] or '-':8s} "
              f"seen={row['docs_seen']} last={row['last_finished_at'] or '-'}")
    return 0


def cmd_rebuild_index(args) -> int:
    if not args.yes:
        print("This drops the Qdrant collection and re-embeds every open-tier document.")
        print("The vault is NOT touched.")
        if input("Type 'rebuild' to continue: ").strip() != "rebuild":
            print("aborted")
            return 1
    qdrant_store.drop_collection()
    with state.writer() as conn:
        conn.execute("DELETE FROM documents WHERE tier=?", (Tier.OPEN.value,))
    qdrant_store.init_collection()
    return _run([s for s in all_sources() if not s.is_vault], force=True, dry_run=False)


def cmd_warm_cache(args) -> int:
    embedder.warm_cache()
    return 0


def cmd_review_quarantine(args) -> int:
    with state.reader() as conn:
        rows = state.open_quarantine(conn)
    if not rows:
        print("Quarantine is empty.")
        return 0
    print(f"{len(rows)} document(s) held back by the sensitivity scan:\n")
    for row in rows:
        print(f"  id={row['id']}  {Path(row['path']).name}")
        print(f"     filed as : {row['domain']}  (from {row['origin_path']})")
        print(f"     tripped  : {row['reason']}")
        print(f"     resolve  : make add FILE={row['path']} DOMAIN=<vault-domain>")
        print(f"                or re-file and re-run ingest to index it as open\n")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.ingest", description="RAG control plane")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="preflight checks").set_defaults(func=cmd_doctor)
    sub.add_parser("warm-cache", help="download and load the embedding models").set_defaults(
        func=cmd_warm_cache
    )

    p = sub.add_parser("ingest", help="sync sources")
    p.add_argument("--source")
    p.add_argument("--domain")
    p.add_argument("--force", action="store_true", help="override the sweep deletion floor")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("reindex", help="force a full re-embed of one source")
    p.add_argument("source_id")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("add", help="file a document into an inbox domain and ingest it")
    p.add_argument("path")
    p.add_argument("--domain", required=True, choices=sorted(DOMAIN_TIERS))
    p.add_argument("--move", action="store_true")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("query", help="search the open tier (direct to Qdrant)")
    p.add_argument("query")
    p.add_argument("--domains")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--dense", action="store_true", help="disable hybrid, dense only")
    p.add_argument("--include-superseded", action="store_true")
    p.add_argument("--include-stale", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("eval", help="rank known answers in open-tier search")
    p.add_argument("--file", default="tests/retrieval/questions.yaml")
    p.add_argument("--depth", type=int, default=10, help="how far down to look for the answer")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("rebuild-index")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_rebuild_index)

    sub.add_parser("review-quarantine").set_defaults(func=cmd_review_quarantine)

    # --- lifecycle ---
    p = sub.add_parser("supersede", help="mark one document as replaced by another")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--domain")
    p.set_defaults(func=cmd_supersede)

    p = sub.add_parser("stale", help="mark a document out of date, now or on a date")
    p.add_argument("uri")
    p.add_argument("--after", help="schedule instead: mark stale on or after this date")
    p.add_argument("--reason")
    p.add_argument("--domain")
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser("retract", help="delete a document's vectors and keep it out for good")
    p.add_argument("uri")
    p.add_argument("--reason", required=True)
    p.add_argument("--domain")
    p.set_defaults(func=cmd_retract)

    p = sub.add_parser("restore", help="clear a lifecycle flag")
    p.add_argument("uri")
    p.add_argument("--domain")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("list", help="every open-tier document")
    p.add_argument("--domain")
    p.add_argument("--match", help="substring of the uri")
    p.add_argument("--all", action="store_true", help="include superseded/stale/retracted")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("lifecycle", help="documents that are not plainly active")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_lifecycle)

    # Vault commands live in app/vaultctl.py: they run inside the vault
    # container, which deliberately lacks this module's qdrant dependency.

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SourceConfigError as exc:
        print(f"sources.yaml: {exc}", file=sys.stderr)
        return 1
    except UnknownDomainError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
