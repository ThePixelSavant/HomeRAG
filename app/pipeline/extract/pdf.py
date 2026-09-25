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
"""

from __future__ import annotations

import logging
import re
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

MODE_RAW = "raw"
MODE_READING = "reading"

_TOKEN_RE = re.compile(r"\S+")

# Longer tokens are left alone. Real glued runs are a line of text at most;
# past this they are dot leaders on a contents page, where splitting helps
# nothing and the search is quadratic in the token's length.
_RESPACE_MAX_LEN = 120

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
        for page in range(1, page_count + 1):
            raw = _page_text(path, page, MODE_RAW)
            reading = _page_text(path, page, MODE_READING)
            text = _respace(raw, reading).strip()

            if not text:
                continue
            total_chars += len(text)
            blocks.append(
                TextBlock(text=text, kind="page", extra={"page": page, "extract_mode": MODE_RAW})
            )

        doc.extra["page_count"] = page_count

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
