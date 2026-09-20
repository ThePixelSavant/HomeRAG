"""Vault control CLI.

Separate from app/ingest.py on purpose. That module imports qdrant_store at
module level, and the vault image deliberately does not install qdrant-client --
keeping the open tier's dependencies out of the container that holds the key.
So vault commands get their own entry point that imports nothing from the open
tier, and the isolation stays enforced by the dependency set rather than by
convention.

Runs inside the vault container via `docker exec`, because that is the process
holding the key in memory.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from app.vault import control


def _send(request: dict) -> dict:
    try:
        return control.send(request)
    except control.ControlUnavailable as exc:
        raise SystemExit(str(exc)) from None


def cmd_unlock(args) -> int:
    if not sys.stdin.isatty():
        raise SystemExit(
            "unlock needs a terminal. Run it as: make unlock\n"
            "The passphrase is deliberately not accepted from a flag, a file or an "
            "environment variable. That is the mechanism that stops a model from "
            "unlocking the vault: a model has no terminal."
        )
    passphrase = getpass.getpass("Vault passphrase: ")
    if not passphrase:
        raise SystemExit("aborted")
    reply = _send({"action": "unlock", "passphrase": passphrase, "ttl": args.ttl})
    if not reply.get("ok"):
        raise SystemExit(reply.get("error", "unlock failed"))
    print(f"Vault unlocked for {reply['seconds_remaining']}s.")
    return 0


def cmd_lock(args) -> int:
    _send({"action": "lock"})
    print("Vault sealed; key wiped.")
    return 0


def cmd_status(args) -> int:
    print(json.dumps(_send({"action": "status"}).get("status"), indent=2, default=str))
    return 0


def cmd_query(args) -> int:
    from app.vault import service

    domains = args.domains.split(",") if args.domains else None
    hits = service.search(args.query, domains=domains, limit=args.limit, ctx=service.Context())
    if args.json:
        print(json.dumps(hits, indent=2, default=str))
        return 0
    if not hits:
        print("no results")
        return 0
    for hit in hits:
        flat = " ".join(hit["content"].split())
        print(f"\n[{hit['score']:.4f}] {hit['title']}  ({hit['domain']})")
        print(f"  {hit['uri']}  chunk {hit['chunk_index']}")
        print(f"  {flat[:260]}")
    return 0


def cmd_ledger(args) -> int:
    from app.vault import service

    filters = {k: v for k, v in (("merchant", args.merchant), ("category", args.category)) if v}
    result = service.query_ledger(
        start_date=args.start, end_date=args.end, group_by=args.group_by,
        limit=args.limit, ctx=service.Context(), **filters,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_lifecycle(args) -> int:
    from app import documents
    from app.vault import service

    kwargs = {"domain": args.domain, "reason": args.reason}
    if args.lifecycle == documents.SUPERSEDED:
        if not args.superseded_by:
            raise SystemExit("supersede needs --superseded-by <uri>")
        kwargs["superseded_by"] = args.superseded_by
        kwargs["reason"] = args.reason or f"replaced by {args.superseded_by}"
    if args.lifecycle == documents.RETRACTED and not args.reason:
        raise SystemExit("retract needs --reason")

    try:
        touched = service.set_lifecycle(args.uri, args.lifecycle, **kwargs)
    except (LookupError, service.VaultSealed) as exc:
        raise SystemExit(str(exc)) from None

    for uri in touched:
        print(f"{uri}: {args.lifecycle}")
    if args.lifecycle == documents.RETRACTED:
        print("Chunks, vectors and the encrypted original are gone. The row remains as a "
              "tombstone so ingestion does not bring it back.")
    return 0


def cmd_approve(args) -> int:
    pending = _send({"action": "pending"}).get("pending", [])
    if not args.code:
        if not pending:
            print("Nothing awaiting approval.")
            return 0
        for grant in pending:
            print(f"  code={grant['code']}  {grant['tool']}  by {grant['principal']}")
            print(f"     chat={grant['chat_id']} message={grant['message_id']}")
            print(f"     query: {grant['query_preview']}\n")
        return 0

    match = next((g for g in pending if g["code"] == args.code), None)
    if match is None:
        raise SystemExit(f"No pending request with code {args.code} (it may have expired).")

    # Show exactly what is being released and require a typed confirmation. A
    # reflexive y/n is no defence against a prompt-injected model, which is the
    # one thing this gate exists to catch.
    print("\nRelease vault data for this request?\n")
    print(f"  principal : {match['principal']}")
    print(f"  tool      : {match['tool']}")
    print(f"  chat      : {match['chat_id']}  message: {match['message_id']}")
    print(f"  query     : {match['query_preview']}\n")
    print("  Confirm the model in that chat is the local one. A frontier model would")
    print("  send these results off this machine.\n")
    if not sys.stdin.isatty():
        raise SystemExit("approve needs a terminal. Run it as: make approve CODE=...")
    if input("Type 'yes' to approve: ").strip().lower() != "yes":
        print("denied")
        return 1
    reply = _send({"action": "approve", "code": args.code})
    if not reply.get("ok"):
        raise SystemExit(reply.get("error", "approval failed"))
    print("Approved. This grant is single-use and expires shortly.")
    return 0


def cmd_audit(args) -> int:
    reply = _send({"action": "audit", "limit": args.limit})
    if not reply.get("ok"):
        raise SystemExit(reply.get("error", "audit unavailable"))
    rows = reply["rows"]
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("No recorded attempts.")
        return 0
    for row in rows:
        print(f"{row['ts']}  {row['decision']:16s} {row['tool']:14s} "
              f"{row['principal'] or '-':38s} rows={row['rows_returned']}")
        if row["reason"]:
            print(f"     {row['reason']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.vaultctl", description="Vault control")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("unlock", help="unlock the vault (TTY only)")
    p.add_argument("--ttl", type=int, default=None)
    p.set_defaults(func=cmd_unlock)

    sub.add_parser("lock").set_defaults(func=cmd_lock)
    sub.add_parser("status").set_defaults(func=cmd_status)

    p = sub.add_parser("query", help="search the vault as a human (no approval needed)")
    p.add_argument("query")
    p.add_argument("--domains")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("ledger")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--merchant")
    p.add_argument("--category")
    p.add_argument("--group-by", dest="group_by")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_ledger)

    # Lifecycle. CLI only, and there is deliberately no MCP tool for it: a
    # model may not decide what counts as true.
    p = sub.add_parser("lifecycle", help="flag a vault document stale/superseded/retracted")
    p.add_argument("uri")
    p.add_argument("lifecycle", choices=["active", "stale", "superseded", "retracted"])
    p.add_argument("--reason")
    p.add_argument("--superseded-by", dest="superseded_by")
    p.add_argument("--domain")
    p.set_defaults(func=cmd_lifecycle)

    p = sub.add_parser("approve")
    p.add_argument("--code")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("audit")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
