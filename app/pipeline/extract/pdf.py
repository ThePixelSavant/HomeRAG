"""PDF extraction via pdftotext, with explicit detection of the OCR gap.

Pages come out individually so page numbers survive into the payload and a
citation can say "page 12".

A scanned PDF has no text layer, and pdftotext returns near-nothing for it.
Embedding that would be worse than skipping it: a near-empty chunk becomes a
high-similarity vector that matches almost any query. So such documents are
marked `ocr_required` and NOT embedded, and `make status` reports the backlog
rather than letting the index look complete when it is not.

### Why every page is extracted twice

`pdftotext -layout` preserves the visual grid, which is what keeps a table row
or a numbered step on one line. On a two-column page it is actively wrong: it
interleaves the columns line by line, so a safety warning ends up spliced into
the middle of unrelated body text. Dropping `-layout` gives reading order,
which fixes the columns and breaks the rows.

Neither mode is correct for a whole manual, because a manual is both. So each
page is extracted both ways and classified by shape -- see `_classify_page`.
The cost is one extra subprocess per page, measured in milliseconds against an
embedding pass measured in seconds.
"""

from __future__ import annotations

import logging
import re
import statistics
import subprocess
from pathlib import Path

from app.pipeline.chunker import TextBlock
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

# Page layout verdicts. `extract_mode` records which extraction was kept, so a
# later pass can tell a deliberate choice from a coin flip.
ROWS = "rows"
COLUMNS = "columns"
PROSE = "prose"
AMBIGUOUS = "ambiguous"

MODE_LAYOUT = "layout"
MODE_READING = "reading"

# A gutter is this many consecutive spaces. Two is too loose -- ordinary
# sentence spacing and justified text both produce it.
_GUTTER_RE = re.compile(r"\s{3,}")

# Median width of the text before the first gutter. Row structure puts
# something short on the left (a step number, a part label, a torque value);
# column prose puts half a line of text there. Measured against the corpus:
# rows land at 1-4, columns at 59-69, and the band between is genuinely
# undecidable from whitespace alone.
LEFT_ROWS_MAX = 20
LEFT_COLUMNS_MIN = 40

# Fewer gutter lines than this and there is no evidence either way, so the page
# is prose and reading order is right by default.
MIN_GUTTER_LINES = 3

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


def _page_text(path: Path, page: int, mode: str = MODE_LAYOUT) -> str:
    """One page, either layout-preserving or in reading order."""
    argv = ["pdftotext"]
    if mode == MODE_LAYOUT:
        argv.append("-layout")
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


def _classify_page(layout_text: str) -> tuple[str, float | None]:
    """Decide whether a `-layout` page is rows, columns, or undecidable.

    Returns the verdict and the median left-segment width that produced it.

    Only the `-layout` rendering is measured, because it is the one that
    preserves horizontal position; reading order has already discarded the
    evidence. The discriminator is how much text sits to the left of the first
    gutter on each line.
    """
    widths = []
    for line in layout_text.splitlines():
        line = line.rstrip()
        # Measure from the first non-space character, not from column 0. A
        # numbered step is indented ("   1    Turn on the computer."), so its
        # first run of 3+ spaces is the indent itself -- counting that as the
        # gutter scores the line as having nothing on the left and drops it.
        start = len(line) - len(line.lstrip())
        if start >= len(line):
            continue
        match = _GUTTER_RE.search(line, start)
        if match is not None:
            widths.append(match.start() - start)

    if len(widths) < MIN_GUTTER_LINES:
        return PROSE, None

    median = statistics.median(widths)
    if median <= LEFT_ROWS_MAX:
        return ROWS, median
    if median >= LEFT_COLUMNS_MIN:
        return COLUMNS, median
    return AMBIGUOUS, median


class PdfExtractor:
    name = "pdftotext"

    def matches(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def extract(self, path: Path) -> RawDoc:
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

        blocks, total_chars = [], 0
        flagged: list[int] = []
        for page in range(1, page_count + 1):
            layout = _page_text(path, page, MODE_LAYOUT)
            verdict, median = _classify_page(layout)

            if verdict == ROWS:
                text, mode = layout.strip(), MODE_LAYOUT
            else:
                # Columns, prose, and ambiguous all take reading order. For
                # ambiguous that is a default, not a decision: manuals are
                # mostly prose, so it is the safer half of a coin flip. The
                # page is recorded either way so the Phase 2 VLM pass has a
                # work queue instead of a guess nobody can find again.
                text, mode = _page_text(path, page, MODE_READING).strip(), MODE_READING
                if verdict == AMBIGUOUS:
                    flagged.append(page)

            if not text:
                continue
            total_chars += len(text)
            blocks.append(
                TextBlock(
                    text=text,
                    kind="page",
                    extra={"page": page, "extract_mode": mode, "layout_verdict": verdict},
                )
            )
            if median is not None:
                logger.debug("%s page %d: %s (median left %.1f)", path.name, page, verdict, median)

        doc.extra["page_count"] = page_count
        if flagged:
            doc.extra["flagged_pages"] = flagged

        if not blocks:
            doc.status = OCR_REQUIRED
            doc.status_detail = f"no text layer across {page_count} pages"
            return doc

        chars_per_page = total_chars / page_count
        if chars_per_page < MIN_CHARS_PER_PAGE:
            doc.status = OCR_REQUIRED
            doc.status_detail = (
                f"{chars_per_page:.0f} chars/page over {page_count} pages, below the "
                f"{MIN_CHARS_PER_PAGE} threshold -- likely scanned"
            )
            # Keep the blocks: a later OCR pass can merge rather than redo.
            doc.blocks = blocks
            return doc

        doc.blocks = blocks
        doc.status = OK if total_chars else EMPTY
        return doc
