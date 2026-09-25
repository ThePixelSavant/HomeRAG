"""Token-accurate chunking.

`chunk_size` is measured in TOKENS, using the embedding model's own tokenizer.
Characters would be a guess, and a guess that runs over the model's 512-token
ceiling is truncated silently with no error.

The whole text is tokenized ONCE and sliced by token index. The obvious
alternative -- re-tokenizing every candidate substring while hunting for a
boundary -- is O(n^2) and takes minutes on a large PDF.

Two things beyond plain splitting happen here:

- **Table headers are repeated.** A torque table that splits across chunks
  leaves every chunk after the first as bare numbers -- `| Bolt M27 | 37 |`
  with nothing saying which column is torque. The header row is re-emitted at
  the top of each continuation chunk and charged to that chunk's budget.
- **Char offsets are kept**, so a chunk can report the lines it came from and
  a citation can point at somewhere specific in the file.
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

# A markdown table separator: only pipes, dashes, colons and whitespace, with
# at least one pipe and a run of dashes. Deliberately strict -- a prose line
# that happens to contain a dash must not turn the paragraph above it into a
# table header.
_TABLE_SEP_CHARS = set("|-: \t")

# A repeated header that eats more than this fraction of the budget is costing
# more context than it restores, so the chunk goes without one.
_HEADER_BUDGET_RATIO = 0.5

# Per-block keys that chunk_blocks combines across a chunk, or reads for
# itself, rather than letting the last block win.
_AGGREGATED = frozenset({"page", "extract_mode", "heading_path", "section_start"})

# A section this small does not get a chunk of its own; the next section is
# packed in behind it. It is almost always a chapter title sitting directly
# above its first subsection, and a chunk holding only a title matches every
# query about the chapter while answering none of them.
SECTION_MIN_TOKENS = 40


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


@dataclass(frozen=True)
class _Table:
    """A markdown pipe table's extent, in char offsets."""

    start: int  # the header row
    body_start: int  # the first data row, i.e. just past the separator line
    end: int  # one past the last data row
    header: str  # header row + separator line, no trailing newline


@dataclass(frozen=True)
class _Span:
    """One emitted piece, with its offsets in the ORIGINAL text."""

    start: int
    end: int
    text: str


def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    return (
        bool(stripped)
        and "|" in stripped
        and stripped.count("-") >= 2
        and set(stripped) <= _TABLE_SEP_CHARS
    )


def find_tables(text: str) -> list[_Table]:
    """Locate markdown pipe tables: a row line followed by a `|---|` separator."""
    lines = text.splitlines(keepends=True)
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line)
    starts.append(pos)  # sentinel, so starts[j] is valid at j == len(lines)

    tables: list[_Table] = []
    i = 0
    while i + 1 < len(lines):
        if "|" in lines[i] and _is_table_separator(lines[i + 1]):
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                j += 1
            tables.append(
                _Table(
                    start=starts[i],
                    body_start=starts[i + 2],
                    end=starts[j],
                    header=lines[i].rstrip("\r\n") + "\n" + lines[i + 1].rstrip("\r\n"),
                )
            )
            i = j
        else:
            i += 1
    return tables


def _header_for(tables: list[_Table], char_pos: int) -> str | None:
    """The header a chunk starting at `char_pos` is missing, if any.

    Returns None when the position is outside every table, or inside one but at
    or before its header -- in that case the chunk already carries it.
    """
    for table in tables:
        if table.body_start <= char_pos < table.end:
            return table.header
    return None


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


def _split_spans(
    raw: str,
    *,
    target: int | None = None,
    overlap: int | None = None,
    hard_max: int | None = None,
    tables: bool = True,
) -> list[_Span]:
    """Split text into pieces, keeping each piece's offsets in `raw`.

    Offsets are relative to `raw` as passed in, including any leading
    whitespace this function strips, so a caller can turn them into line
    numbers without redoing the arithmetic.
    """
    target = target if target is not None else settings.chunk_target_tokens
    overlap = overlap if overlap is not None else settings.chunk_overlap_tokens
    hard_max = hard_max if hard_max is not None else settings.chunk_max_tokens
    if target > hard_max:
        raise ValueError(f"target {target} exceeds hard_max {hard_max}")

    text = raw.strip()
    if not text:
        return []
    lead = len(raw) - len(raw.lstrip())

    offsets = encode_offsets(text)
    if not offsets:
        return []
    if len(offsets) <= hard_max:
        return [_Span(lead, lead + len(text), text)]

    starts = [start for start, _ in offsets]
    boundaries = _boundaries(text, starts)
    total = len(offsets)
    spans_of_tables = find_tables(text) if tables else []
    header_budget = int(target * _HEADER_BUDGET_RATIO)

    spans: list[_Span] = []
    i = 0
    while i < total:
        # A chunk that starts inside a table has to re-state the header, and
        # those tokens come out of this chunk's budget rather than pushing it
        # over the model's ceiling.
        header = _header_for(spans_of_tables, offsets[i][0])
        reserve = 0
        if header:
            reserve = count_tokens(header + "\n")
            if reserve > header_budget:
                header, reserve = None, 0

        ceiling = min(i + target - reserve, total)
        if ceiling <= i:
            ceiling = min(i + 1, total)
        if ceiling < total:
            floor = i + int((target - reserve) * _SNAP_FLOOR_RATIO)
            end = _snap(boundaries, floor, ceiling)
        else:
            end = ceiling
        end = min(end, i + hard_max - reserve, total)
        if end <= i:
            end = min(i + target, total)

        piece = text[offsets[i][0] : offsets[end - 1][1]].strip()
        if piece:
            body = f"{header}\n{piece}" if header else piece
            spans.append(_Span(lead + offsets[i][0], lead + offsets[end - 1][1], body))

        if end >= total:
            break
        # Step forward by at least one token so a pathological boundary can
        # never stall the loop.
        i = max(end - overlap, i + 1)

    return spans


def split_text(text: str, **kwargs) -> list[str]:
    """Split text into pieces of at most `hard_max` tokens."""
    return [span.text for span in _split_spans(text, **kwargs)]


def _line_span(text: str, start: int, end: int, offset: int) -> list[int]:
    """1-based [first, last] line numbers for a char range, shifted by `offset`."""
    first = text.count("\n", 0, start) + 1 + offset
    last = text.count("\n", 0, max(start, end - 1)) + 1 + offset
    return [first, last]


def chunk_text(
    text: str,
    *,
    heading_path: list[str] | None = None,
    extra: dict | None = None,
    start_index: int = 0,
    prefix: str = "",
    line_offset: int = 0,
    **kwargs,
) -> list[Chunk]:
    """Chunk prose. `prefix` is prepended to each chunk and charged to its budget.

    `line_offset` is how many lines precede `text` in the file it came from, so
    a section of a larger document still reports document line numbers.
    """
    prefix_tokens = count_tokens(prefix) if prefix else 0
    if prefix_tokens:
        kwargs.setdefault("target", settings.chunk_target_tokens - prefix_tokens)
        kwargs.setdefault("hard_max", settings.chunk_max_tokens - prefix_tokens)

    chunks = []
    for offset, span in enumerate(_split_spans(text, **kwargs)):
        body = prefix + span.text if prefix else span.text
        merged = dict(extra or {})
        merged["locator"] = {"lines": _line_span(text, span.start, span.end, line_offset)}
        chunks.append(
            Chunk(
                index=start_index + offset,
                text=body,
                token_count=count_tokens(body),
                heading_path=list(heading_path or []),
                extra=merged,
            )
        )
    return chunks


def _sections(markdown: str) -> list[tuple[list[str], str, int]]:
    """Split markdown into (heading_path, body, body_offset) on ATX headings.

    `body_offset` is the body's char position in `markdown`, which is what lets
    a chunk of a section report its line numbers in the whole file.
    """
    matches = list(_HEADING_RE.finditer(markdown))
    if not matches:
        return [([], markdown, 0)]

    sections: list[tuple[list[str], str, int]] = []
    head = markdown[: matches[0].start()]
    preamble = head.strip()
    if preamble:
        sections.append(([], preamble, len(head) - len(head.lstrip())))

    path: list[str] = []
    for n, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        path = path[: level - 1]
        while len(path) < level - 1:
            path.append("")
        path.append(title)

        end = matches[n + 1].start() if n + 1 < len(matches) else len(markdown)
        raw = markdown[match.end() : end]
        body = raw.strip()
        if body:
            offset = match.end() + (len(raw) - len(raw.lstrip()))
            sections.append(([p for p in path if p], body, offset))
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
    for heading_path, body, body_offset in _sections(markdown):
        prefix = " > ".join(heading_path) + "\n\n" if heading_path else ""
        section = chunk_text(
            body,
            heading_path=heading_path,
            extra=extra,
            start_index=index,
            prefix=prefix,
            line_offset=markdown.count("\n", 0, body_offset),
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
    section_min_tokens: int = SECTION_MIN_TOKENS,
    crumb_root: str = "",
    **kwargs,
) -> list[Chunk]:
    """Pack atomic blocks into chunks, never splitting one unless it must.

    Used where the unit carries meaning on its own -- a conversation turn, a
    PDF page -- and slicing it in half would leave a question without its
    answer.

    Blocks that carry a `heading_path` (PDF sections) are also kept apart:
    one starting a section (`section_start`) starts a new chunk unless what is
    pending is under `section_min_tokens`, so two sections share a chunk only
    when one of them is a bare title. And each chunk is prefixed with its
    heading breadcrumb, charged to its budget -- the whole path when the chunk
    starts mid-section, only the parents when its text opens on the heading
    itself. `crumb_root` (the document title) heads every breadcrumb: a
    section of a Roland sheet says "Setting the tempo" and never which
    instrument, which is the first thing a question names. Blocks without a
    heading path (transcript turns) pack as before, with no breadcrumb.
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
    pending_crumb_tokens = 0

    def _crumb(block: TextBlock, *, at_heading: bool) -> str:
        if "heading_path" not in block.extra:
            return ""
        path = block.extra["heading_path"]
        if at_heading:
            path = path[:-1]
        path = [crumb_root, *path] if crumb_root else path
        return " > ".join(path) + "\n\n" if path else ""

    def _opening_crumb(block: TextBlock) -> str:
        """The crumb this block carries when it opens a chunk."""
        return _crumb(block, at_heading=bool(block.extra.get("section_start")))

    def _merge(blocks: list[TextBlock]) -> dict:
        """Doc-level extra plus the blocks', with page numbers ACCUMULATED.

        A plain dict.update keeps only the last block's page, so a chunk built
        from pages 11 and 12 would cite page 12 -- and the answer the reader is
        sent to look up is on page 11.
        """
        merged = dict(extra or {})
        for block in blocks:
            merged.update({k: v for k, v in block.extra.items() if k not in _AGGREGATED})
        merged["block_kinds"] = [b.kind for b in blocks]

        pages = sorted({p for b in blocks if (p := b.extra.get("page")) is not None})
        if pages:
            merged["locator"] = {"page": pages[0], "pages": pages}
        modes = sorted({m for b in blocks if (m := b.extra.get("extract_mode"))})
        if modes:
            merged["extract_modes"] = modes
        return merged

    def flush() -> None:
        nonlocal pending, pending_tokens, pending_crumb_tokens, index
        if not pending:
            return
        body = separator.join(b.text for b in pending)
        text = prefix + _opening_crumb(pending[0]) + body
        chunks.append(
            Chunk(
                index=index,
                text=text,
                token_count=count_tokens(text),
                heading_path=list(pending[0].extra.get("heading_path") or []),
                extra=_merge(pending),
            )
        )
        index += 1
        pending, pending_tokens, pending_crumb_tokens = [], 0, 0

    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        tokens = count_tokens(text)
        opening = _opening_crumb(block)
        opening_tokens = count_tokens(opening) if opening else 0

        if tokens + prefix_tokens + opening_tokens > hard_max:
            # Oversized on its own: split just this one. Every piece after the
            # first starts mid-section, so every piece reserves room for the
            # full breadcrumb. A bare title pending ahead of it goes into the
            # first piece rather than becoming a chunk of a few tokens.
            lead = block
            carried: list[TextBlock] = []
            if "heading_path" in block.extra and pending and pending_tokens < section_min_tokens:
                carried, lead = pending, pending[0]
                text = separator.join([b.text for b in carried] + [text])
                pending, pending_tokens, pending_crumb_tokens = [], 0, 0
            else:
                flush()
            merged = _merge([*carried, block])
            opening = _opening_crumb(lead)
            full = _crumb(block, at_heading=False)
            piece_kwargs = dict(kwargs)
            reserve = max(count_tokens(c) if c else 0 for c in (opening, full))
            if reserve:
                piece_kwargs["target"] = target - reserve
                piece_kwargs["hard_max"] = hard_max - reserve
            for n, piece in enumerate(split_text(text, **piece_kwargs)):
                owner = lead if n == 0 else block
                body = prefix + (opening if n == 0 else full) + piece
                chunks.append(
                    Chunk(
                        index=index,
                        text=body,
                        token_count=count_tokens(body),
                        heading_path=list(owner.extra.get("heading_path") or []),
                        extra=dict(merged),
                    )
                )
                index += 1
            continue

        if block.extra.get("section_start") and pending_tokens >= section_min_tokens:
            flush()
        if pending and pending_tokens + sep_tokens + tokens > budget - pending_crumb_tokens:
            flush()
        if not pending:
            pending_crumb_tokens = opening_tokens
        pending.append(block)
        pending_tokens += tokens + (sep_tokens if len(pending) > 1 else 0)

    flush()
    return chunks
