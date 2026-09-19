"""Open-tier vector store.

Holds only open-tier content. Sensitive data never reaches this module -- see
app/vault/ -- because a vector cannot be encrypted and stay searchable, and
inversion recovers most of the source text from one.
"""

from __future__ import annotations

import logging
import uuid

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.config import settings
from app.domains import POINT_NAMESPACE
from app.pipeline import embedder

logger = logging.getLogger(__name__)

DENSE = "dense"
SPARSE = "bm25"

# Payload fields that get an index. Each one costs RAM, and this box shares
# memory with a resident llama-server, so the list stays short: these five are
# the ones actually filtered on.
INDEXED_FIELDS: dict[str, qm.PayloadSchemaType] = {
    "domain": qm.PayloadSchemaType.KEYWORD,
    "source_id": qm.PayloadSchemaType.KEYWORD,
    "doc_id": qm.PayloadSchemaType.KEYWORD,
    "last_seen_run": qm.PayloadSchemaType.KEYWORD,
    "chunk_index": qm.PayloadSchemaType.INTEGER,
}

# Refuse to delete more than this fraction of a source in one sweep without an
# explicit override. A source that enumerates to zero -- bad mount, unreadable
# path, a typo in sources.yaml -- would otherwise wipe itself from the index.
SWEEP_FLOOR = 0.5

_client: QdrantClient | None = None


class FingerprintMismatch(RuntimeError):
    pass


class SweepRefused(RuntimeError):
    pass


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    return _client


def reset_client() -> None:
    global _client
    _client = None


def meta_collection() -> str:
    return f"{settings.collection_name}__meta"


def point_id(source_id: str, doc_id: str, chunk_index: int) -> str:
    """Deterministic, positional point ID.

    Re-running ingestion produces identical IDs, so an upsert overwrites rather
    than duplicating. Deliberately NOT a hash of the chunk's content: that makes
    a document's stale chunks unenumerable and collides whenever two documents
    share a boilerplate paragraph.
    """
    return str(uuid.uuid5(POINT_NAMESPACE, f"{source_id}|{doc_id}|{chunk_index}"))


# --------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------


def _read_fingerprint() -> dict | None:
    client = get_client()
    if not client.collection_exists(meta_collection()):
        return None
    points = client.retrieve(meta_collection(), ids=[0], with_payload=True)
    return points[0].payload if points else None


def _write_fingerprint() -> None:
    client = get_client()
    name = meta_collection()
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=qm.VectorParams(size=1, distance=qm.Distance.COSINE),
        )
    client.upsert(
        collection_name=name,
        points=[qm.PointStruct(id=0, vector=[0.0], payload=embedder.model_fingerprint())],
    )


def verify_fingerprint() -> None:
    """Fail loudly if the embedding space changed under an existing index.

    Dimension alone cannot catch this: bge-base, nomic-v1.5, gte-base and
    arctic-m are all 768-dim. Mixing two models in one collection does not
    error, it just returns quietly wrong results.
    """
    stored = _read_fingerprint()
    current = embedder.model_fingerprint()
    if stored is None:
        return
    diffs = {k: (stored.get(k), v) for k, v in current.items() if stored.get(k) != v}
    if diffs:
        detail = "; ".join(f"{k}: stored={s!r} configured={c!r}" for k, (s, c) in diffs.items())
        raise FingerprintMismatch(
            f"Collection {settings.collection_name!r} was built with a different "
            f"embedding configuration ({detail}). Mixing embedding spaces corrupts "
            "retrieval silently. Run: make rebuild-index"
        )


# --------------------------------------------------------------------------
# Collection lifecycle
# --------------------------------------------------------------------------


def init_collection() -> None:
    client = get_client()
    name = settings.collection_name

    if client.collection_exists(name):
        verify_fingerprint()
        logger.info("Collection %r ready (dim=%d)", name, settings.embed_dim)
        return

    client.create_collection(
        collection_name=name,
        vectors_config={
            DENSE: qm.VectorParams(size=settings.embed_dim, distance=qm.Distance.COSINE)
        },
        # IDF is computed server-side, so we never have to track corpus
        # statistics on the client.
        sparse_vectors_config={
            SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF),
        },
    )
    for field, schema in INDEXED_FIELDS.items():
        # `domain` deliberately does NOT use is_tenant: that co-locates storage
        # to speed up single-tenant reads, which is the opposite of the
        # cross-domain recall this index exists for.
        client.create_payload_index(name, field, schema)

    _write_fingerprint()
    logger.info("Created collection %r (dim=%d, dense+sparse)", name, settings.embed_dim)


def drop_collection() -> None:
    client = get_client()
    for name in (settings.collection_name, meta_collection()):
        if client.collection_exists(name):
            client.delete_collection(name)
            logger.info("Dropped collection %r", name)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


def build_filter(
    domains: list[str] | None = None,
    source_id: str | None = None,
    doc_id: str | None = None,
) -> qm.Filter | None:
    must: list[qm.Condition] = []
    if domains:
        must.append(qm.FieldCondition(key="domain", match=qm.MatchAny(any=list(domains))))
    if source_id:
        must.append(qm.FieldCondition(key="source_id", match=qm.MatchValue(value=source_id)))
    if doc_id:
        must.append(qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id)))
    return qm.Filter(must=must) if must else None


def count_points(query_filter: qm.Filter | None = None) -> int:
    return get_client().count(
        collection_name=settings.collection_name, count_filter=query_filter, exact=True
    ).count


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


def upsert_chunks(points: list[qm.PointStruct]) -> None:
    if not points:
        return
    get_client().upsert(collection_name=settings.collection_name, points=points, wait=True)


def make_point(
    *, source_id: str, doc_id: str, chunk_index: int, dense, sparse, payload: dict
) -> qm.PointStruct:
    return qm.PointStruct(
        id=point_id(source_id, doc_id, chunk_index),
        vector={
            DENSE: dense,
            SPARSE: qm.SparseVector(
                indices=[int(i) for i in sparse.indices],
                values=[float(v) for v in sparse.values],
            ),
        },
        payload=payload,
    )


def existing_chunks(doc_id: str) -> dict[int, str]:
    """{chunk_index: content_hash} for a document, without fetching vectors.

    This is what makes re-ingesting an append-only file cheap: only chunks whose
    hash actually changed get re-embedded.
    """
    client = get_client()
    found: dict[int, str] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=settings.collection_name,
            scroll_filter=build_filter(doc_id=doc_id),
            with_payload=["chunk_index", "content_hash"],
            with_vectors=False,
            limit=256,
            offset=offset,
        )
        for point in points:
            payload = point.payload or {}
            if "chunk_index" in payload:
                found[int(payload["chunk_index"])] = payload.get("content_hash", "")
        if offset is None:
            break
    return found


def touch_doc(doc_id: str, run_id: str) -> None:
    """Refresh a skipped document's run marker without re-embedding it.

    Unchanged documents still have to prove they were seen this run, or the
    disappearance sweep would delete them.
    """
    get_client().set_payload(
        collection_name=settings.collection_name,
        payload={"last_seen_run": run_id},
        points=build_filter(doc_id=doc_id),
        wait=True,
    )


# --------------------------------------------------------------------------
# Deletion -- the two tombstone mechanisms
# --------------------------------------------------------------------------


def delete_doc_tail(doc_id: str, chunk_count: int) -> None:
    """Drop chunks past the document's current length.

    When a document shrinks from 10 chunks to 6, chunks 6-9 would otherwise
    linger forever and keep surfacing in results. Run unconditionally after
    every upsert: it is a no-op when nothing shrank, and it self-heals a prior
    partial failure.
    """
    get_client().delete(
        collection_name=settings.collection_name,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[
                    qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id)),
                    qm.FieldCondition(key="chunk_index", range=qm.Range(gte=chunk_count)),
                ]
            )
        ),
        wait=True,
    )


def delete_doc(doc_id: str) -> None:
    get_client().delete(
        collection_name=settings.collection_name,
        points_selector=qm.FilterSelector(filter=build_filter(doc_id=doc_id)),
        wait=True,
    )


def sweep_source(
    source_id: str, run_id: str, *, enumeration_complete: bool, force: bool = False
) -> int:
    """Delete points from `source_id` that this run did not touch.

    This is the most dangerous operation in the system. A source that
    enumerates to zero -- an empty bind mount, an unreadable path, a typo in
    sources.yaml -- would sweep itself out of the index entirely. Hence two
    guards before anything is deleted, and a caller that must explicitly assert
    its walk completed.
    """
    if not enumeration_complete:
        raise SweepRefused(
            f"Refusing to sweep {source_id!r}: enumeration did not complete. "
            "Deleting on a partial walk would remove documents that still exist."
        )

    source_filter = build_filter(source_id=source_id)
    total = count_points(source_filter)
    if total == 0:
        return 0

    stale_filter = qm.Filter(
        must=[qm.FieldCondition(key="source_id", match=qm.MatchValue(value=source_id))],
        must_not=[qm.FieldCondition(key="last_seen_run", match=qm.MatchValue(value=run_id))],
    )
    stale = count_points(stale_filter)
    if stale == 0:
        return 0

    if not force and stale > total * SWEEP_FLOOR:
        raise SweepRefused(
            f"Refusing to sweep {source_id!r}: {stale} of {total} points are stale "
            f"({stale / total:.0%}, over the {SWEEP_FLOOR:.0%} floor). This usually means "
            "the source enumerated wrongly rather than that it genuinely shrank. "
            "Re-run with --force if the deletion is intended."
        )

    get_client().delete(
        collection_name=settings.collection_name,
        points_selector=qm.FilterSelector(filter=stale_filter),
        wait=True,
    )
    logger.info("Swept %d stale points from source %r", stale, source_id)
    return stale


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def search(
    query: str,
    *,
    limit: int = 5,
    domains: list[str] | None = None,
    source_id: str | None = None,
    hybrid: bool = True,
    candidates: int = 40,
) -> list[dict]:
    """Retrieve open-tier chunks.

    Hybrid by default: dense embeddings are systematically weak on exact rare
    tokens -- a part number, a function name -- which is most of what gets
    asked of a manual or an SDK doc. BM25 is strong there, and fusing the two
    covers both. `client.search()` no longer exists in qdrant-client >= 1.12;
    `query_points` is the replacement and returns a response object.
    """
    client = get_client()
    query_filter = build_filter(domains=domains, source_id=source_id)
    dense = embedder.embed_query(query)

    if hybrid:
        sparse = embedder.sparse_query(query)
        response = client.query_points(
            collection_name=settings.collection_name,
            prefetch=[
                qm.Prefetch(query=dense, using=DENSE, limit=candidates, filter=query_filter),
                qm.Prefetch(
                    query=qm.SparseVector(
                        indices=[int(i) for i in sparse.indices],
                        values=[float(v) for v in sparse.values],
                    ),
                    using=SPARSE,
                    limit=candidates,
                    filter=query_filter,
                ),
            ],
            query=qm.FusionQuery(fusion=qm.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
    else:
        response = client.query_points(
            collection_name=settings.collection_name,
            query=dense,
            using=DENSE,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )

    results = []
    for hit in response.points:
        payload = hit.payload or {}
        results.append(
            {
                "score": hit.score,
                "content": payload.get("content", ""),
                "title": payload.get("title", ""),
                "uri": payload.get("uri", ""),
                "domain": payload.get("domain", ""),
                "source_id": payload.get("source_id", ""),
                "doc_id": payload.get("doc_id", ""),
                "chunk_index": payload.get("chunk_index"),
                "heading_path": payload.get("heading_path", []),
                "indexed_at": payload.get("indexed_at", ""),
            }
        )
    return results
