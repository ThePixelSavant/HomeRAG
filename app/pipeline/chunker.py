"""Token-accurate chunking.

`chunk_size` is measured in TOKENS, using the embedding model's own tokenizer.
Characters would be a guess, and a guess that runs over the model's 512-token
ceiling is truncated silently with no error.

The whole text is tokenized ONCE and sliced by token index. The obvious
alternative -- re-tokenizing every candidate substring while hunting for a
boundary -- is O(n^2) and takes minutes on a large PDF.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from app.config import settings
from app.pipeline.embedder import count_tokens, encode_offsets

# Boundary preferences, highest first. A chunk that ends at a paragraph break
# reads better than one that ends mid-sentence, so we snap backwards to the
# best boundary available rather than cutting at exactly N tokens.
_PARA_RE = re.compile(r"\n\s*\n")
_LINE_RE = re.compile(r"\n")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")

_PRIORITY_PARA = 3
_PRIORITY_LINE = 2
_PRIORITY_SENT = 1

# How far back from the target a boundary may be before we give up and cut
# hard. Snapping back further than this wastes too much of the window.
_SNAP_FLOOR_RATIO = 0.5

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)


@dataclass
class Chunk:
    index: int
    text: str
    token_count: int
    heading_path: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class TextBlock:
    """An atomic unit that should not be split across chunks if avoidable."""

    text: str
    kind: str = "prose"
    extra: dict = field(default_factory=dict)


def _boundaries(text: str, starts: list[int]) -> dict[int, int]:
    """Map token index -> boundary priority, for snapping chunk ends."""
    found: dict[int, int] = {}
    for regex, priority in (
        (_PARA_RE, _PRIORITY_PARA),
        (_LINE_RE, _PRIORITY_LINE),
        (_SENT_RE, _PRIORITY_SENT),
    ):
        for match in regex.finditer(text):
            token_idx = bisect.bisect_left(starts, match.end())
            if 0 < token_idx < len(starts):
                found[token_idx] = max(found.get(token_idx, 0), priority)
    return found


def _snap(boundaries: dict[int, int], floor: int, ceiling: int) -> int:
    """Best boundary in (floor, ceiling], else ceiling."""
    best_idx, best_priority = ceiling, 0
    for idx, priority in boundaries.items():
        if floor < idx <= ceiling and (
            priority > best_priority or (priority == best_priority and idx > best_idx)
        ):
            best_idx, best_priority = idx, priority
    return best_idx


def split_text(
    text: str,
    *,
    target: int | None = None,
    overlap: int | None = None,
    hard_max: int | None = None,
) -> list[str]:
    """Split text into pieces of at most `hard_max` tokens."""
    target = target if target is not None else settings.chunk_target_tokens
    overlap = overlap if overlap is not None else settings.chunk_overlap_tokens
    hard_max = hard_max if hard_max is not None else settings.chunk_max_tokens
    if target > hard_max:
        raise ValueError(f"target {target} exceeds hard_max {hard_max}")

    text = text.strip()
    if not text:
        return []

    offsets = encode_offsets(text)
    if not offsets:
        return []
    if len(offsets) <= hard_max:
        return [text]

    starts = [start for start, _ in offsets]
    boundaries = _boundaries(text, starts)
    total = len(offsets)

    pieces: list[str] = []
    i = 0
    while i < total:
        ceiling = min(i + target, total)
        if ceiling < total:
            floor = i + int(target * _SNAP_FLOOR_RATIO)
            end = _snap(boundaries, floor, ceiling)
        else:
            end = ceiling
        end = min(end, i + hard_max, total)
        if end <= i:
            end = min(i + target, total)

        piece = text[offsets[i][0] : offsets[end - 1][1]].strip()
        if piece:
            pieces.append(piece)

        if end >= total:
            break
        # Step forward by at least one token so a pathological boundary can
        # never stall the loop.
        i = max(end - overlap, i + 1)

    return pieces


def chunk_text(
    text: str,
    *,
    heading_path: list[str] | None = None,
    extra: dict | None = None,
    start_index: int = 0,
    prefix: str = "",
    **kwargs,
) -> list[Chunk]:
    """Chunk prose. `prefix` is prepended to each chunk and charged to its budget."""
    prefix_tokens = count_tokens(prefix) if prefix else 0
    if prefix_tokens:
        kwargs.setdefault("target", settings.chunk_target_tokens - prefix_tokens)
        kwargs.setdefault("hard_max", settings.chunk_max_tokens - prefix_tokens)

    chunks = []
    for offset, piece in enumerate(split_text(text, **kwargs)):
        body = prefix + piece if prefix else piece
        chunks.append(
            Chunk(
                index=start_index + offset,
                text=body,
                token_count=count_tokens(body),
                heading_path=list(heading_path or []),
                extra=dict(extra or {}),
            )
        )
    return chunks


def _sections(markdown: str) -> list[tuple[list[str], str]]:
    """Split markdown into (heading_path, body) on ATX headings."""
    matches = list(_HEADING_RE.finditer(markdown))
    if not matches:
        return [([], markdown)]

    sections: list[tuple[list[str], str]] = []
    preamble = markdown[: matches[0].start()].strip()
    if preamble:
        sections.append(([], preamble))

    path: list[str] = []
    for n, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        path = path[: level - 1]
        while len(path) < level - 1:
            path.append("")
        path.append(title)

        end = matches[n + 1].start() if n + 1 < len(matches) else len(markdown)
        body = markdown[match.end() : end].strip()
        if body:
            sections.append(([p for p in path if p], body))
    return sections


def chunk_markdown(
    markdown: str, *, extra: dict | None = None, start_index: int = 0, **kwargs
) -> list[Chunk]:
    """Chunk markdown, carrying the heading breadcrumb into the embedded text.

    A chunk about torque specs is close to meaningless without knowing which
    assembly it belongs to, and the section heading is usually the only place
    that says so. Prepending the breadcrumb costs a few tokens and recovers
    that context, so it is charged against the chunk budget.
    """
    chunks: list[Chunk] = []
    index = start_index
    for heading_path, body in _sections(markdown):
        prefix = " > ".join(heading_path) + "\n\n" if heading_path else ""
        section = chunk_text(
            body,
            heading_path=heading_path,
            extra=extra,
            start_index=index,
            prefix=prefix,
            **kwargs,
        )
        chunks.extend(section)
        index += len(section)
    return chunks


def chunk_blocks(
    blocks: list[TextBlock],
    *,
    extra: dict | None = None,
    start_index: int = 0,
    separator: str = "\n\n",
    prefix: str = "",
    **kwargs,
) -> list[Chunk]:
    """Pack atomic blocks into chunks, never splitting one unless it must.

    Used where the unit carries meaning on its own -- a conversation turn, a
    PDF page -- and slicing it in half would leave a question without its
    answer.
    """
    target = kwargs.get("target", settings.chunk_target_tokens)
    hard_max = kwargs.get("hard_max", settings.chunk_max_tokens)
    prefix_tokens = count_tokens(prefix) if prefix else 0
    budget = target - prefix_tokens
    sep_tokens = count_tokens(separator)

    chunks: list[Chunk] = []
    index = start_index
    pending: list[TextBlock] = []
    pending_tokens = 0

    def flush() -> None:
        nonlocal pending, pending_tokens, index
        if not pending:
            return
        body = separator.join(b.text for b in pending)
        text = prefix + body if prefix else body
        merged = dict(extra or {})
        kinds = [b.kind for b in pending]
        merged["block_kinds"] = kinds
        for block in pending:
            merged.update(block.extra)
        chunks.append(
            Chunk(index=index, text=text, token_count=count_tokens(text), extra=merged)
        )
        index += 1
        pending, pending_tokens = [], 0

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        tokens = count_tokens(text)

        if tokens + prefix_tokens > hard_max:
            # Oversized on its own: flush what we have, then split just this one.
            flush()
            for piece in split_text(text, **kwargs):
                body = prefix + piece if prefix else piece
                merged = dict(extra or {})
                merged.update(block.extra)
                merged["block_kinds"] = [block.kind]
                chunks.append(
                    Chunk(
                        index=index,
                        text=body,
                        token_count=count_tokens(body),
                        extra=merged,
                    )
                )
                index += 1
            continue

        if pending and pending_tokens + sep_tokens + tokens > budget:
            flush()
        pending.append(block)
        pending_tokens += tokens + (sep_tokens if len(pending) > 1 else 0)

    flush()
    return chunks
