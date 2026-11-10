"""Tenants of a shared Qdrant (ADR-035).

One Qdrant can hold several people's open-tier indexes. Each tenant gets a
collection pair (`<name>` and `<name>__meta`) and tokens that Qdrant itself
confines to that pair: reads, writes and snapshots of their own collections
succeed, and everything else -- another tenant's collection, a collection that
does not exist, creating, deleting or recovering one -- is refused by Qdrant,
not by this code. Verified against Qdrant 1.20 with JWT RBAC on.

Every token is HS256-signed with the server's MAIN key, which therefore mints
anything: it stays with the admin. Qdrant rejects tokens signed with the
read-only key, so mcp-server and rag-admin, which hold that, cannot mint.

Revocation is a registry row, not a key rotation. Each token carries a
`value_exists` claim that holds only while `rag_tenants` has a row with the
tenant's name and the generation the token was issued under. Revoking writes a
new generation, which kills every token issued before it and none of anyone
else's. Tenants cannot read the registry.

The generation is a nanosecond timestamp rather than a counter so that a lost or
recreated registry never revives an old token: a fresh row gets a fresh
generation, and every tenant needs a new token, which is the safe way to fail.
For the same reason the registry is not in backups -- restoring an old copy
would undo every revocation made since.

Admin commands only. They run in the worker that holds the main key, and
refuse to run under a tenant token.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone

import jwt
from qdrant_client import models as qm

from app.config import settings
from app.pipeline import qdrant_store

REGISTRY = "rag_tenants"
# Lowercase, starts with a letter, no "__" (that suffix is the meta collection).
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,47}$")
ACCESS = {"read": "r", "write": "rw"}
DEFAULT_TOKEN_DAYS = 365
MAX_TOKEN_DAYS = 730
# Registry row ids derive from this. Changing it orphans every row.
_ROW_NAMESPACE = uuid.UUID("6c1f8b1e-3b0a-4f43-9a55-0b8e3f2a7d11")


class TenantError(RuntimeError):
    pass


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name) or "__" in name:
        raise TenantError(
            f"{name!r} is not a tenant name: 2-48 characters, lowercase letters, digits, "
            "'-' and '_', starting with a letter, no '__'."
        )
    reserved = {REGISTRY, settings.collection_name}
    if name in reserved:
        raise TenantError(f"{name!r} is reserved on this server.")
    return name


def _admin_client():
    if not settings.qdrant_api_key:
        raise TenantError("QDRANT_API_KEY is unset; tenant commands need the server's main key.")
    if qdrant_store.token_claims() is not None:
        raise TenantError(
            "This install holds a Qdrant token, not the server's main key. Tenant "
            "commands run on the machine that hosts Qdrant."
        )
    return qdrant_store.get_client()


def _row_id(name: str) -> str:
    return str(uuid.uuid5(_ROW_NAMESPACE, name))


def _new_generation(previous: int | None = None) -> int:
    # Nanoseconds, so a row recreated moments after a token was issued still
    # gets a different value; strictly increasing within one registry.
    return max(time.time_ns(), (previous or 0) + 1)


def _ensure_registry(client) -> None:
    if not client.collection_exists(REGISTRY):
        client.create_collection(
            collection_name=REGISTRY,
            vectors_config=qm.VectorParams(size=1, distance=qm.Distance.COSINE),
        )


def _row(client, name: str) -> dict | None:
    if not client.collection_exists(REGISTRY):
        return None
    points = client.retrieve(REGISTRY, ids=[_row_id(name)], with_payload=True)
    return dict(points[0].payload) if points else None


def _write_row(client, name: str, generation: int, created_at: str) -> None:
    client.upsert(
        collection_name=REGISTRY,
        points=[
            qm.PointStruct(
                id=_row_id(name),
                vector=[1.0],
                payload={"tenant": name, "generation": generation, "created_at": created_at},
            )
        ],
        wait=True,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(name: str) -> None:
    """Create a tenant's collections, fingerprint them, and register the tenant.

    The vectors take this server's embedding settings: a tenant indexes with
    the same model, and its worker's fingerprint check refuses a different one.
    """
    validate_name(name)
    client = _admin_client()
    for collection in (name, qdrant_store.meta_collection(name)):
        if client.collection_exists(collection):
            raise TenantError(f"Collection {collection!r} already exists.")
    _ensure_registry(client)
    if _row(client, name) is not None:
        raise TenantError(f"Tenant {name!r} is already registered.")
    qdrant_store.create_collection(name)
    _write_row(client, name, _new_generation(), _now())


def issue_token(name: str, *, access: str = "write", days: int = DEFAULT_TOKEN_DAYS) -> tuple[str, int]:
    """A token for `name`'s two collections. Returns (token, expires_at).

    "write" is for the tenant's ingestion worker, "read" for their MCP server
    and dashboard. Every token expires: Qdrant accepts one without `exp`
    forever.
    """
    validate_name(name)
    if access not in ACCESS:
        raise TenantError(f"access must be one of {sorted(ACCESS)}")
    if not 1 <= days <= MAX_TOKEN_DAYS:
        raise TenantError(f"days must be 1-{MAX_TOKEN_DAYS}")
    client = _admin_client()
    row = _row(client, name)
    if row is None:
        raise TenantError(f"No tenant {name!r}. Create it with `rag tenant-create {name}`.")
    now = int(time.time())
    expires = now + days * 86400
    claims = {
        "sub": name,
        "iat": now,
        "exp": expires,
        "access": [
            {"collection": c, "access": ACCESS[access]}
            for c in (name, qdrant_store.meta_collection(name))
        ],
        "value_exists": {
            "collection": REGISTRY,
            "matches": [
                {"key": "tenant", "value": name},
                {"key": "generation", "value": row["generation"]},
            ],
        },
    }
    return jwt.encode(claims, settings.qdrant_api_key, algorithm="HS256"), expires


def revoke(name: str) -> None:
    """Invalidate every token issued to `name` so far. New ones can be issued after.

    Works when the row is missing too (a lost registry): it writes a fresh one.
    """
    validate_name(name)
    client = _admin_client()
    _ensure_registry(client)
    row = _row(client, name)
    if row is None and not client.collection_exists(name):
        raise TenantError(f"No tenant {name!r}.")
    previous = row["generation"] if row else None
    _write_row(client, name, _new_generation(previous), row["created_at"] if row else _now())


def list_tenants() -> list[dict]:
    client = _admin_client()
    if not client.collection_exists(REGISTRY):
        return []
    rows, offset = [], None
    while True:
        points, offset = client.scroll(REGISTRY, limit=256, offset=offset, with_payload=True)
        rows.extend(dict(p.payload) for p in points)
        if offset is None:
            break
    out = []
    for row in sorted(rows, key=lambda r: r["tenant"]):
        name = row["tenant"]
        exists = client.collection_exists(name)
        out.append(
            {
                "tenant": name,
                "created_at": row.get("created_at"),
                "tokens_valid_since": datetime.fromtimestamp(
                    row["generation"] / 1e9, timezone.utc
                ).isoformat(timespec="seconds"),
                "points": client.count(name, exact=True).count if exists else None,
            }
        )
    return out


def delete(name: str) -> list[str]:
    """Drop a tenant's collections and registry row. Their tokens die with the row."""
    validate_name(name)
    client = _admin_client()
    dropped = []
    if client.collection_exists(REGISTRY) and _row(client, name) is not None:
        client.delete(
            collection_name=REGISTRY,
            points_selector=qm.PointIdsList(points=[_row_id(name)]),
            wait=True,
        )
        dropped.append(f"{REGISTRY} row")
    for collection in (name, qdrant_store.meta_collection(name)):
        if client.collection_exists(collection):
            client.delete_collection(collection)
            dropped.append(collection)
    if not dropped:
        raise TenantError(f"No tenant {name!r}.")
    return dropped
