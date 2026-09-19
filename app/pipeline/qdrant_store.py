import logging
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    VectorParams,
)
from app.config import settings

logger = logging.getLogger(__name__)

_client: QdrantClient | None = None


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=settings.qdrant_url)
    return _client


def init_collection() -> None:
    """Create the Qdrant collection, or verify its dimension matches EMBED_DIM.

    The embedding dimension is frozen at collection creation time. If EMBED_DIM
    does not match an existing collection, startup fails — run `make rebuild-index`
    to drop and recreate the collection with the new model.
    """
    client = get_client()
    name = settings.collection_name

    if client.collection_exists(name):
        info = client.get_collection(name)
        vec_config = info.config.params.vectors
        if isinstance(vec_config, dict):
            raise RuntimeError(
                f"Collection '{name}' uses named vectors, which this stack does not support. "
                "Run: make rebuild-index"
            )
        actual_dim = vec_config.size
        if actual_dim != settings.embed_dim:
            raise RuntimeError(
                f"Collection '{name}' has dimension {actual_dim} but EMBED_DIM={settings.embed_dim}. "
                "Changing the embedding model requires dropping and rebuilding the collection. "
                "Run: make rebuild-index"
            )
        logger.info("Collection '%s' exists (dim=%d)", name, actual_dim)
        return

    client.create_collection(
        collection_name=name,
        vectors_config=VectorParams(size=settings.embed_dim, distance=Distance.COSINE),
    )
    client.create_payload_index(name, "source_id", PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "source_type", PayloadSchemaType.KEYWORD)
    logger.info("Created collection '%s' (dim=%d)", name, settings.embed_dim)


def search(
    query_vector: list[float],
    limit: int = 5,
    source_id: str | None = None,
) -> list[dict]:
    client = get_client()
    query_filter = None
    if source_id:
        query_filter = Filter(
            must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))]
        )

    hits = client.search(
        collection_name=settings.collection_name,
        query_vector=query_vector,
        limit=limit,
        query_filter=query_filter,
        with_payload=True,
    )
    return [
        {
            "score": h.score,
            "content": h.payload.get("content", ""),
            "source_url": h.payload.get("source_url", ""),
            "source_id": h.payload.get("source_id", ""),
            "last_indexed": h.payload.get("last_indexed", ""),
            "chunk_id": str(h.id),
        }
        for h in hits
    ]


def drop_collection() -> None:
    client = get_client()
    name = settings.collection_name
    if client.collection_exists(name):
        client.delete_collection(name)
        logger.info("Dropped collection '%s'", name)
