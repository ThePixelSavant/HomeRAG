"""The single embedding authority.

Every vector in this system -- open tier and vault, ingest time and query time --
is produced here. Nothing else imports fastembed. That is what keeps the model,
the prefixes and the normalization from drifting between the process that writes
vectors and the process that searches them.

Two fastembed behaviours are load-bearing and easy to get wrong:

1. Dense `query_embed`/`passage_embed` do NOT apply text prefixes. In
   `text_embedding_base.py` both simply delegate to `embed`; only
   JinaEmbeddingV3 overrides them, and it does so with an ONNX task id rather
   than a prefix. So prefixing is ours to do, and we do it here, once.

2. Sparse BM25 `query_embed` IS meaningfully different from `embed`: it assigns
   every token a weight of 1.0 instead of a term frequency. Queries must use it.

Getting (1) wrong silently degrades retrieval. Getting (2) wrong silently
degrades it too. Neither raises.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastembed import SparseTextEmbedding, TextEmbedding
from tokenizers import Tokenizer

from app.config import settings
from app.domains import SCHEMA_VERSION

logger = logging.getLogger(__name__)

SPARSE_MODEL = "Qdrant/bm25"
DISTANCE = "cosine"

# The model's hard input ceiling. Input beyond this is truncated by the model
# with no error and no warning, so we assert rather than let it pass.
MODEL_MAX_TOKENS = 512

# Per-model prefix conventions. bge-*-v1.5 is explicitly documented as not
# needing them; nomic's are mandatory. Keeping the table here means a model
# swap is a one-line change that the collection fingerprint will then enforce.
_PREFIXES: dict[str, tuple[str, str]] = {
    # model_name: (query_prefix, passage_prefix)
    "BAAI/bge-small-en-v1.5": ("", ""),
    "BAAI/bge-base-en-v1.5": ("", ""),
    "BAAI/bge-large-en-v1.5": ("", ""),
    "nomic-ai/nomic-embed-text-v1.5": ("search_query: ", "search_document: "),
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
}


def prefixes() -> tuple[str, str]:
    """(query_prefix, passage_prefix) for the configured model."""
    try:
        return _PREFIXES[settings.embed_model]
    except KeyError:
        raise RuntimeError(
            f"No prefix convention recorded for {settings.embed_model!r}. "
            "Add it to _PREFIXES -- guessing here corrupts retrieval silently."
        ) from None


@lru_cache(maxsize=2)
def _dense(threads: int) -> TextEmbedding:
    logger.info(
        "Loading dense model %s (threads=%d, cache=%s)",
        settings.embed_model,
        threads,
        settings.fastembed_cache_path,
    )
    return TextEmbedding(
        model_name=settings.embed_model,
        cache_dir=str(settings.fastembed_cache_path),
        threads=threads,
    )


@lru_cache(maxsize=2)
def _sparse(threads: int) -> SparseTextEmbedding:
    return SparseTextEmbedding(
        model_name=SPARSE_MODEL,
        cache_dir=str(settings.fastembed_cache_path),
        threads=threads,
    )


@lru_cache(maxsize=1)
def _tokenizer() -> Tokenizer:
    """A truncation-free copy of the model's own tokenizer.

    fastembed configures its tokenizer with truncation at the model's max
    length. Counting tokens with that instance would cap every measurement at
    512 and make chunking blind to anything longer, so we rebuild an
    unconstrained copy from its serialized form rather than mutating the one
    the embedding path depends on.
    """
    inner = _dense(settings.embed_threads_ingest).model.tokenizer
    if inner is None:  # pragma: no cover - only with lazy_load=True
        raise RuntimeError("Tokenizer unavailable; the dense model failed to load.")
    clone = Tokenizer.from_str(inner.to_str())
    clone.no_truncation()
    clone.no_padding()
    return clone


def count_tokens(text: str) -> int:
    return len(_tokenizer().encode(text, add_special_tokens=False).ids)


def encode_offsets(text: str) -> list[tuple[int, int]]:
    """Character spans for each token, so chunking can slice by token index.

    The alternative -- re-tokenizing every candidate substring while searching
    for a boundary -- is O(n^2) and takes minutes on a large PDF.
    """
    return _tokenizer().encode(text, add_special_tokens=False).offsets


def _check_budget(texts: list[str]) -> None:
    for text in texts:
        n = count_tokens(text)
        if n > MODEL_MAX_TOKENS:
            raise ValueError(
                f"Text is {n} tokens, over the model's {MODEL_MAX_TOKENS}-token ceiling. "
                "The model would truncate it silently. Chunk it first."
            )


def embed_passages(texts: list[str], threads: int | None = None) -> list[list[float]]:
    """Embed documents. Applies the passage prefix itself -- callers must not."""
    if not texts:
        return []
    _, passage_prefix = prefixes()
    prepared = [passage_prefix + t for t in texts]
    _check_budget(prepared)
    model = _dense(threads if threads is not None else settings.embed_threads_ingest)
    return [v.tolist() for v in model.embed(prepared)]


def embed_query(text: str, threads: int | None = None) -> list[float]:
    """Embed a query. Applies the query prefix itself -- callers must not."""
    query_prefix, _ = prefixes()
    prepared = query_prefix + text
    _check_budget([prepared])
    model = _dense(threads if threads is not None else settings.embed_threads_query)
    return next(iter(model.embed([prepared]))).tolist()


def sparse_passages(texts: list[str], threads: int | None = None):
    """BM25 term-frequency vectors for documents."""
    if not texts:
        return []
    model = _sparse(threads if threads is not None else settings.embed_threads_ingest)
    return list(model.embed(texts))


def sparse_query(text: str, threads: int | None = None):
    """BM25 query vector.

    Uses `query_embed`, which -- unlike the dense case -- is genuinely different
    from `embed`: it weights every token 1.0 rather than by term frequency.
    """
    model = _sparse(threads if threads is not None else settings.embed_threads_query)
    return next(iter(model.query_embed(text)))


def model_fingerprint() -> dict:
    """Identity of the embedding space, stored alongside the vectors.

    Dimension alone cannot detect a model swap -- bge-base, nomic-v1.5, gte-base
    and arctic-m are all 768-dim -- so the model name and prefixes travel too.
    """
    query_prefix, passage_prefix = prefixes()
    return {
        "embed_model": settings.embed_model,
        "embed_dim": settings.embed_dim,
        "sparse_model": SPARSE_MODEL,
        "query_prefix": query_prefix,
        "passage_prefix": passage_prefix,
        "distance": DISTANCE,
        "schema_version": SCHEMA_VERSION,
    }


def warm_cache() -> None:
    """Force both models to download and load. Run once after a fresh install."""
    _dense(settings.embed_threads_ingest)
    _sparse(settings.embed_threads_ingest)
    dim = len(embed_query("warm"))
    if dim != settings.embed_dim:
        raise RuntimeError(
            f"{settings.embed_model} produced {dim}-dim vectors but EMBED_DIM={settings.embed_dim}."
        )
    logger.info("Model cache warm: %s (%d-dim) + %s", settings.embed_model, dim, SPARSE_MODEL)
