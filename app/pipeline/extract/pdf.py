"""PDF extraction via pdftotext, with explicit detection of the OCR gap.

Pages come out individually so page numbers survive into the payload and a
citation can say "page 12".

A scanned PDF has no text layer, and pdftotext returns near-nothing for it.
Embedding that would be worse than skipping it: a near-empty chunk becomes a
high-similarity vector that matches almost any query. So such documents are
marked `ocr_required` and NOT embedded, and `make status` reports the backlog
rather than letting the index look complete when it is not.

### Why `-raw`, and why every page is still extracted twice

`pdftotext` has three orderings, and two of them read geometry:

- `-layout` preserves the visual grid. It keeps a table row on one line, and
  on any multi-column page it interleaves the columns line by line.
- The default "reading order" guesses columns from geometry. It gets simple
  two-column pages right and scrambles denser ones -- on Roland's A3 manual
  sheets it split a settings table from its own rows.
- `-raw` emits text in the order the PDF stores it. Publishing tools store a
  text frame's content in the frame's own order, so columns come out whole,
  numbered steps stay in sequence, and table rows mostly stay on one line.

Measured across the 382 pages of the manuals indexed so far (four
publishers), `-raw` was never worse than the other two on any page compared
side by side, including the ones the old whitespace classifier chose `-layout`
for, and it kept 90-97% of real table rows. Its one defect is word spacing: it takes the
gaps between words from the stored glyphs rather than from their positions,
so text set with letter-spacing comes out glued -- "INJURYORDEATH.THISPUMP"
on a pump manual's safety page. Reading order has the same characters with
the spaces restored from geometry, so each page is extracted both ways and the
reading-order words are used to re-split raw's glued ones (`_respace`). The
cost is one extra subprocess per page, measured in milliseconds.

What `-raw` cannot fix is a PDF whose stored order is itself scrambled. None
seen yet; the Phase 2 VLM pass is the answer if one turns up.

### Why pages are cut into sections

A Roland manual page is thousands of tokens holding a dozen short sections.
Cut by size alone, one chunk held arpeggio, chord memory, Key Transpose and
Assign Mode together, and its embedding matched none of them: the answer to
"how do I set the SH-01A to play as a polysynth?" ranked 8th. So each page is
split at its section headings and the chunker starts a chunk at each one.

Headings are found by FONT SIZE, from `pdftohtml -xml` (the same poppler that
provides `pdftotext`). As text they are indistinguishable from table cells --
`MONO Monophonic` looks like a heading -- but every manual indexed sets its
headings at least 2pt above body text. See `_headings` for the filters.

### Why contents pages are dropped

A table of contents names every topic in the manual, so it matches every
query on vocabulary and answers none of them. Worse, its entries are set at
heading size, so the section split cut each line into a chunk of its own: 195
of the first 1,182 chunks were contents lines, and "torque specification"
returned two of them in the top three. `_is_contents_page` finds them by
their dot leaders; see there for why that cannot catch a spec table.

### Pages the text layer cannot serve

Given a `transcribe.PagePlan`, pages without a text layer -- and pages a
human named -- are read by the vision model instead, as markdown whose
headings feed the same section split. See transcribe.py and ADR-030.
"""

from __future__ import annotations

import html
import logging
import re
import subprocess
from collections import Counter
from pathlib import Path

from app.pipeline import vlm
from app.pipeline.chunker import TextBlock
from app.pipeline.extract import transcribe
from app.pipeline.extract.base import (
    BLOCKS,
    EMPTY,
    EXTRACT_FAILED,
    OCR_REQUIRED,
    OK,
    RawDoc,
)

logger = logging.getLogger(__name__)

# Below this many characters per page, assume there is no text layer.
MIN_CHARS_PER_PAGE = 100

MODE_RAW = "raw"
MODE_READING = "reading"
# A page read by the vision model (see transcribe.py).
MODE_VLM = "vlm"

_TOKEN_RE = re.compile(r"\S+")

# Longer tokens are left alone. Real glued runs are a line of text at most;
# past this they are dot leaders on a contents page, where splitting helps
# nothing and the search is quadratic in the token's length.
_RESPACE_MAX_LEN = 120

# Heading detection, measured on the six manuals indexed so far: body text is
# 9-15pt, headings 12-30pt, and every one of them sits at least 2pt above the
# body. Anything longer than 80 chars is a sentence at display size, not a
# heading.
HEADING_MIN_DELTA = 2
HEADING_MAX_CHARS = 80
# `9:00`, `iii` and lone step numbers are set at heading size in contact boxes
# and page furniture; a real heading has words.
HEADING_MIN_LETTERS = 3
# A size used fewer times than this is a cover title (`SH-01A` at 24pt, once),
# not a level of structure.
HEADING_MIN_USES = 3
# In a document of at least this many pages, a heading size whose uses span
# less than this fraction of it is front matter, not a level (see
# `_parse_headings`). Short documents -- a two-page Roland sheet -- are exempt:
# there every size spans nothing.
HEADING_SPAN_MIN_PAGES = 10
HEADING_MIN_SPAN = 0.1
# No page-margin filter for running headers: in every manual indexed, headers
# and footers are set at or below body size, and the only headings in the top
# margin were real ones -- chapter titles, and section titles at the top of an
# A3 sheet's columns. Excluding the margin dropped 66 of Analog Lab's.

_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_FONTSPEC_RE = re.compile(r"<fontspec ([^>]*)/>")
_PAGE_RE = re.compile(r"<page ([^>]*)>(.*?)</page>", re.DOTALL)
_TEXT_RE = re.compile(r"<text ([^>]*)>(.*?)</text>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_LETTER_RE = re.compile(r"[^\W\d_]")

# A contents line ends in a dot leader -- packed (Arturia) or spaced (Dell) --
# and a bare page number. Measured on the seven manuals indexed so far, every
# page with such a line is a contents page and every contents page is at least
# 80% of them; the rest are wrapped titles and running headers.
_CONTENTS_LINE_RE = re.compile(r"(?:\.\s?){4,}\s*(\d{1,4})\s*$")
CONTENTS_MIN_LINES = 5
CONTENTS_MIN_SHARE = 0.5
# `-raw` reads a two-column contents page in stored order, so its page numbers
# only mostly ascend: Analog Lab's steps back three times in 36. A spec table
# that happened to end its rows in bare integers would ascend about half the
# time.
CONTENTS_MIN_ASCENDING = 0.75

_PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.MULTILINE)
_TITLE_RE = re.compile(r"^Title:\s+(.+)$", re.MULTILINE)


class PdfToTextMissing(RuntimeError):
    pass


def _pdfinfo(path: Path) -> tuple[int, str | None]:
    try:
        out = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, timeout=60
        ).stdout
    except FileNotFoundError:
        return 0, None
    except subprocess.SubprocessError:
        return 0, None
    pages = _PAGES_RE.search(out)
    title = _TITLE_RE.search(out)
    return (int(pages.group(1)) if pages else 0, title.group(1).strip() if title else None)


def _page_text(path: Path, page: int, mode: str = MODE_RAW) -> str:
    """One page, either in stored order (`-raw`) or pdftotext's reading order."""
    argv = ["pdftotext"]
    if mode == MODE_RAW:
        argv.append("-raw")
    argv += ["-enc", "UTF-8", "-f", str(page), "-l", str(page), str(path), "-"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise PdfToTextMissing(
            "pdftotext not found. Install poppler-utils (it is in Dockerfile.ingest)."
        ) from None
    except subprocess.SubprocessError as exc:
        logger.warning("pdftotext failed on %s page %d: %s", path, page, exc)
        return ""
    return result.stdout


def _respace(raw: str, reading: str) -> str:
    """Restore the word breaks `-raw` dropped, using reading order's words.

    Both renderings hold the same characters, so a raw token that reading
    order never produced, but that is a concatenation of words it did, is a
    glued run: split it back into those words. The split uses the fewest
    pieces, which keeps a real compound whole when reading order has it.
    Tokens reading order also produced are never touched.
    """
    vocab = set(_TOKEN_RE.findall(reading))

    def split(token: str) -> str:
        n = len(token)
        if token in vocab or n > _RESPACE_MAX_LEN:
            return token
        # best[i]: fewest vocabulary words that spell token[i:], or None.
        best: list[list[str] | None] = [None] * (n + 1)
        best[n] = []
        for i in range(n - 1, -1, -1):
            for j in range(n, i, -1):
                tail = best[j]
                if tail is not None and token[i:j] in vocab:
                    if best[i] is None or len(tail) + 1 < len(best[i]):
                        best[i] = [token[i:j], *tail]
        pieces = best[0]
        return " ".join(pieces) if pieces and len(pieces) > 1 else token

    return _TOKEN_RE.sub(lambda m: split(m.group()), raw)


def _is_contents_page(text: str, page_count: int) -> bool:
    """Whether a page is a table of contents: mostly leader lines whose page
    numbers exist in the document and mostly ascend."""
    lines = [line for line in text.splitlines() if line.strip()]
    numbers = [int(m.group(1)) for line in lines if (m := _CONTENTS_LINE_RE.search(line))]
    if len(numbers) < CONTENTS_MIN_LINES or len(numbers) < CONTENTS_MIN_SHARE * len(lines):
        return False
    if not all(1 <= n <= page_count for n in numbers):
        return False
    ascending = sum(a <= b for a, b in zip(numbers, numbers[1:]))
    return ascending >= CONTENTS_MIN_ASCENDING * (len(numbers) - 1)


def _squash(text: str) -> str:
    """A line's identity for heading matching: `-raw` and `pdftohtml` agree
    on the characters of a heading but not always on its spaces."""
    return "".join(text.split())


def _pdftohtml_xml(path: Path) -> str:
    argv = ["pdftohtml", "-xml", "-i", "-q", "-enc", "UTF-8", "-stdout", str(path)]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning("pdftohtml unavailable for %s, chunking without headings: %s", path, exc)
        return ""
    return result.stdout


def _parse_headings(xml: str) -> dict[int, list[tuple[str, int]]]:
    """Page number -> [(heading text, level)] in page order; level 1 is largest.

    Fragments on one line (same page, top and size) are joined first:
    pdftohtml can emit `1.1.` and `Making the connections` separately where
    `-raw` has them on one line.
    """
    sizes = {a["id"]: int(a["size"]) for a in (dict(_ATTR_RE.findall(m)) for m in _FONTSPEC_RE.findall(xml))}

    lines: list[tuple[int, int, str]] = []  # page, size, text
    chars_at: Counter[int] = Counter()
    for page_attrs, body in _PAGE_RE.findall(xml):
        page = int(dict(_ATTR_RE.findall(page_attrs))["number"])
        last_key = None
        for text_attrs, raw_text in _TEXT_RE.findall(body):
            attr = dict(_ATTR_RE.findall(text_attrs))
            size = sizes.get(attr.get("font", ""), 0)
            text = html.unescape(_TAG_RE.sub("", raw_text)).strip()
            if not text:
                continue
            chars_at[size] += len(text)
            top = int(attr.get("top", 0))
            key = (page, top, size)
            if key == last_key:
                lines[-1] = (page, size, f"{lines[-1][2]} {text}")
            else:
                lines.append((page, size, text))
            last_key = key

    if not chars_at:
        return {}
    body_size = chars_at.most_common(1)[0][0]
    candidates = [
        (page, size, text)
        for page, size, text in lines
        if size >= body_size + HEADING_MIN_DELTA
        and len(text) <= HEADING_MAX_CHARS
        and len(_LETTER_RE.findall(text)) >= HEADING_MIN_LETTERS
    ]
    uses = Counter(size for _, size, _ in candidates)
    kept = sorted((s for s, n in uses.items() if n >= HEADING_MIN_USES), reverse=True)

    # Front matter is often set a size ABOVE the chapter titles -- Arturia's
    # "Table Of Contents" and "Special Thanks" are 17pt over 15pt chapters --
    # and as a higher level it would sit at the root of every breadcrumb in
    # the manual. A size confined to a few pages of a long document is not a
    # level of structure, so it shares the level of the next size down.
    pages_of: dict[int, set[int]] = {}
    for page, size, _ in candidates:
        pages_of.setdefault(size, set()).add(page)
    last_page = max((page for page, _, _ in lines), default=0)

    def front_matter(size: int) -> bool:
        pages = pages_of[size]
        return last_page >= HEADING_SPAN_MIN_PAGES and max(pages) - min(pages) < last_page * HEADING_MIN_SPAN

    structural = [s for s in kept if not front_matter(s)] or kept
    levels: dict[int, int] = {}
    for size in kept:
        below = [s for s in structural if s <= size]
        levels[size] = structural.index(below[0]) + 1 if below else len(structural)
    found: dict[int, list[tuple[str, int]]] = {}
    for page, size, text in candidates:
        if size in levels:
            found.setdefault(page, []).append((text, levels[size]))
    return found


def _headings(path: Path) -> dict[int, list[tuple[str, int]]]:
    return _parse_headings(_pdftohtml_xml(path))


Section = tuple[str, list[str], bool]  # text, heading path, starts at a heading


def _split_sections(
    text: str, headings: list[tuple[str, int]], path: list[tuple[int, str]]
) -> tuple[list[Section], list[tuple[int, str]]]:
    """Cut one page's text at its headings.

    Returns the sections and the heading path to carry to the next page. Text
    before the page's first heading continues the previous page's section.

    A line is a heading only if its text matches one the font data found on
    THIS page, and only as many times as the font data found it -- so a
    heading's words repeated in body text further down do not cut again.
    Consecutive heading lines at one level are one wrapped heading.
    """
    level_of: dict[str, int] = {}
    remaining: Counter[str] = Counter()
    for heading, level in headings:
        key = _squash(heading)
        level_of[key] = min(level, level_of.get(key, level))
        remaining[key] += 1

    sections: list[Section] = []
    current: list[str] = []
    starts = False

    def flush() -> None:
        body = "\n".join(current).strip()
        if body:
            sections.append((body, [title for _, title in path], starts))

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        key = _squash(lines[i])
        if not (key and remaining[key] > 0):
            current.append(lines[i])
            i += 1
            continue

        level = level_of[key]
        title = [lines[i]]
        remaining[key] -= 1
        i += 1
        while i < len(lines):
            nxt = _squash(lines[i])
            if not (nxt and remaining[nxt] > 0 and level_of[nxt] == level):
                break
            title.append(lines[i])
            remaining[nxt] -= 1
            i += 1

        flush()
        path = [(lvl, t) for lvl, t in path if lvl < level]
        path.append((level, " ".join(line.strip() for line in title)))
        current, starts = list(title), True

    flush()
    return sections, path


def _transcribe_pages(
    path: Path, layers: dict[int, tuple[str, str]], plan: transcribe.PagePlan, doc: RawDoc
) -> dict[int, str]:
    """Page -> markdown, for the pages the vision model read successfully.

    Requested pages always go; pages without a text layer go automatically
    unless more of them than `plan.max_auto_pages` would need a model call, in
    which case none do and the document says so. Cached readings cost nothing
    and are always used. After the first time the model cannot be reached,
    the rest of the document is not attempted: every further page would wait
    out the same failure.
    """
    requested = [p for p in layers if plan.is_requested(p)]
    auto = [
        p for p, (text, _) in layers.items()
        if plan.auto and not plan.is_requested(p) and transcribe.lacks_text(text, MIN_CHARS_PER_PAGE)
    ]
    uncached = [p for p in auto if p not in plan.cached]
    deferred = 0
    if len(uncached) > plan.max_auto_pages:
        deferred = len(uncached)
        auto = [p for p in auto if p in plan.cached]

    readings: dict[int, str] = {}
    outcomes: dict[int, str] = {}
    unreachable = False
    for page in sorted({*requested, *auto}):
        text, layer = layers[page]
        result = plan.cached.get(page)
        if result is None:
            if unreachable:
                outcomes[page] = transcribe.FAILED
                continue
            try:
                # Only a page with a real text layer can be checked against it.
                result = transcribe.transcribe(
                    path, page, layer, check=not transcribe.lacks_text(text, MIN_CHARS_PER_PAGE)
                )
            except vlm.VisionError as exc:
                logger.warning("%s page %d: vision model unavailable, not transcribed: %s", path.name, page, exc)
                unreachable = True
                outcomes[page] = transcribe.FAILED
                continue
            except (subprocess.SubprocessError, OSError, KeyError) as exc:
                logger.warning("%s page %d: not transcribed: %s", path.name, page, exc)
                outcomes[page] = transcribe.FAILED
                continue
            plan.produced[page] = result
        if result.status == transcribe.REJECTED:
            logger.warning("%s page %d: transcription rejected, %s", path.name, page, result.detail)
        outcomes[page] = result.status
        if result.status == transcribe.USED:
            readings[page] = result.text

    if outcomes or deferred:
        doc.extra["transcription"] = {"pages": outcomes, "deferred": deferred}
    return readings


class PdfExtractor:
    name = "pdftotext"

    def matches(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def extract(self, path: Path, plan: transcribe.PagePlan | None = None) -> RawDoc:
        """Extract every page; with a `plan`, some pages come from the vision
        model instead of the text layer. Without one, nothing is transcribed
        -- the caller decides, because only it knows where the cache lives."""
        stat = path.stat()
        doc = RawDoc(
            rel_uri=path.name,
            strategy=BLOCKS,
            mtime=stat.st_mtime,
            size_bytes=stat.st_size,
            extractor=self.name,
        )

        page_count, title = _pdfinfo(path)
        doc.title = title or path.stem

        if page_count == 0:
            doc.status = EXTRACT_FAILED
            doc.status_detail = "pdfinfo reported no pages"
            return doc

        # No headings found -- pdftohtml missing, or a document without a
        # heading size -- means one block per page, exactly as before.
        headings = _headings(path)
        heading_path: list[tuple[int, str]] = []

        # Every page's text layer first: the vision pass needs all of them to
        # decide which pages to send, and to check what comes back.
        layers: dict[int, tuple[str, str]] = {}
        for page in range(1, page_count + 1):
            raw = _page_text(path, page, MODE_RAW)
            reading = _page_text(path, page, MODE_READING)
            layers[page] = (_respace(raw, reading).strip(), f"{raw}\n{reading}")
        transcribed = _transcribe_pages(path, layers, plan, doc) if plan is not None else {}

        blocks, total_chars, contents = [], 0, []
        for page in range(1, page_count + 1):
            if page in transcribed:
                text, page_headings = transcribe.markdown_headings(transcribed[page])
                mode = MODE_VLM
            else:
                text, page_headings, mode = layers[page][0], headings.get(page, []), MODE_RAW

            if not text:
                continue
            # Counted before the skip: a contents page has a text layer, and
            # the scanned-PDF check below must not think otherwise.
            total_chars += len(text)
            if _is_contents_page(text, page_count):
                contents.append(page)
                continue
            if not headings and mode == MODE_RAW:
                blocks.append(
                    TextBlock(text=text, kind="page", extra={"page": page, "extract_mode": MODE_RAW})
                )
                continue

            # A transcribed page brings its own headings, as markdown, so it
            # is cut into sections even in a document with no font headings.
            sections, heading_path = _split_sections(text, page_headings, heading_path)
            for body, crumb, starts in sections:
                blocks.append(
                    TextBlock(
                        text=body,
                        kind="section",
                        extra={
                            "page": page,
                            "extract_mode": mode,
                            "heading_path": crumb,
                            "section_start": starts,
                        },
                    )
                )

        doc.extra["page_count"] = page_count
        if contents:
            logger.info("%s: skipped contents pages %s", path.name, contents)

        info = doc.extra.get("transcription", {})
        deferred = info.get("deferred", 0)
        waiting = (
            f"; {deferred} page(s) need the vision model, over the automatic limit of "
            f"{plan.max_auto_pages} -- rag transcribe URI={path.name} PAGES=all"
            if deferred and plan is not None else ""
        )
        # Every page without a text layer has been read or found blank, so a
        # thin document is genuinely thin, not unread. Calling it
        # ocr_required would keep it out of the index and buy its pages again
        # on every run.
        settled = (
            plan is not None and plan.auto and not deferred
            and transcribe.FAILED not in (info.get("pages") or {}).values()
        )
        if not blocks:
            if settled:
                doc.status = EMPTY
                doc.status_detail = f"no text on any of {page_count} pages, vision model included"
                return doc
            doc.status = OCR_REQUIRED
            doc.status_detail = f"no text layer across {page_count} pages{waiting}"
            return doc

        chars_per_page = total_chars / page_count
        if chars_per_page < MIN_CHARS_PER_PAGE and not settled:
            doc.status = OCR_REQUIRED
            doc.status_detail = (
                f"{chars_per_page:.0f} chars/page over {page_count} pages, below the "
                f"{MIN_CHARS_PER_PAGE} threshold -- likely scanned{waiting}"
            )
            # Keep the blocks: a later OCR pass can merge rather than redo.
            doc.blocks = blocks
            return doc

        doc.blocks = blocks
        doc.status = OK if total_chars else EMPTY
        return doc
