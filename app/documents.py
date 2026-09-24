"""Document-level concepts shared by both tiers: lifecycle, citations, and the
shape of a result row as a model sees it.

Deliberately dependency-free, like app/domains.py. The vault image does not
install qdrant-client and the open MCP server cannot import app/vault/, so
anything both tiers must agree on has to live somewhere neither one owns.

### Lifecycle

Being replaced is only one of the ways a document stops being worth trusting,
and the four cases need different retrieval behaviour:

- `active`      the default
- `superseded`  a NAMED newer document replaced it; returned only on opt-in
- `stale`       out of date with no replacement; opt-in, and flagged in the text
- `retracted`   wrong, or should not be here at all; never returned, vectors deleted

`retracted` is enforced by absence rather than by a filter. Its points are
deleted from Qdrant and its rows from the vault, so no query path has to
remember to exclude it. What remains is a tombstone row in the state database,
which is what stops the next `make ingest` re-indexing the file that is still
sitting on disk.
"""

from __future__ import annotations

ACTIVE = "active"
STALE = "stale"
SUPERSEDED = "superseded"
RETRACTED = "retracted"

LIFECYCLES = (ACTIVE, STALE, SUPERSEDED, RETRACTED)

# States whose state-database row must outlive the document's chunks: the row
# IS the exclusion, so the disappearance sweep must not collect it.
TOMBSTONED = frozenset({RETRACTED})

# States a search returns without being asked to.
DEFAULT_VISIBLE = frozenset({ACTIVE})


class UnknownLifecycleError(ValueError):
    def __init__(self, value: str) -> None:
        super().__init__(f"Unknown lifecycle {value!r}. Valid: {', '.join(LIFECYCLES)}.")
        self.value = value


def validate(value: str) -> str:
    if value not in LIFECYCLES:
        raise UnknownLifecycleError(value)
    return value


def visible_lifecycles(*, include_superseded: bool = False, include_stale: bool = False) -> list[str]:
    """Which lifecycles a query should match.

    `retracted` is never in the result and has no opt-in, by design.
    """
    allowed = set(DEFAULT_VISIBLE)
    if include_superseded:
        allowed.add(SUPERSEDED)
    if include_stale:
        allowed.add(STALE)
    return sorted(allowed)


def stale_banner(reason: str | None, since: str | None) -> str:
    """The warning prepended to stale content at READ time.

    Never stored: writing it into the payload would change the embedded text
    and the content hash, which would turn flipping a flag into a re-embed.
    """
    detail = f" since {since}" if since else ""
    return f"[STALE{detail}: {reason or 'marked out of date'}]"


def format_citation(uri: str, locator: dict | None) -> str:
    """A human-checkable pointer, e.g. `pump.pdf, page 12`.

    A chunk that spans pages cites the FIRST one. The answer is where the
    passage starts; citing the last page sends the reader past it.
    """
    if not uri:
        return ""
    if not locator:
        return uri

    page = locator.get("page")
    if page is not None:
        pages = locator.get("pages") or [page]
        if len(pages) > 1:
            return f"{uri}, pages {pages[0]}-{pages[-1]}"
        return f"{uri}, page {page}"

    lines = locator.get("lines")
    if lines:
        first, last = lines[0], lines[-1]
        return f"{uri}, line {first}" if first == last else f"{uri}, lines {first}-{last}"

    anchor = locator.get("anchor")
    if anchor:
        return f"{uri}#{anchor}"

    return uri


def for_model(hit: dict) -> dict:
    """The fields of a result row that are worth a model's context window.

    Every token of a tool result is prefilled before the model writes a word,
    and on the CPU-only llama-server that is ~70 tokens/s. The full row spent
    ~250 tokens per hit on metadata the model cannot use: `locator`, `uri` and
    `title` restate the citation, `heading_path` is already the first line of a
    markdown chunk's content, and the ids and timestamps are bookkeeping.

    Kept: the content, the citation to quote, and the `doc_id`/`chunk_index`
    pair `fetch_context` needs. Lifecycle is sent only when it is news -- a
    default search returns nothing but `active`. The CLI keeps the full row.

    Rows that are not hits (an `error` from a rejected request) pass through.
    """
    if "content" not in hit:
        return hit
    out = {
        "content": hit["content"],
        "citation": hit.get("citation", ""),
        "doc_id": hit.get("doc_id", ""),
        "chunk_index": hit.get("chunk_index"),
    }
    if hit.get("score") is not None:
        out["score"] = round(hit["score"], 3)
    if hit.get("lifecycle", ACTIVE) != ACTIVE:
        out["lifecycle"] = hit["lifecycle"]
    if hit.get("superseded_by"):
        out["superseded_by"] = hit["superseded_by"]
    return out
