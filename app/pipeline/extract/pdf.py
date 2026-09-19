"""PDF extraction via pdftotext, with explicit detection of the OCR gap.

Pages come out individually so page numbers survive into the payload and a
citation can say "page 12".

A scanned PDF has no text layer, and pdftotext returns near-nothing for it.
Embedding that would be worse than skipping it: a near-empty chunk becomes a
high-similarity vector that matches almost any query. So such documents are
marked `ocr_required` and NOT embedded, and `make status` reports the backlog
rather than letting the index look complete when it is not.
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


def _page_text(path: Path, page: int) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", "-f", str(page), "-l", str(page),
             str(path), "-"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise PdfToTextMissing(
            "pdftotext not found. Install poppler-utils (it is in Dockerfile.ingest)."
        ) from None
    except subprocess.SubprocessError as exc:
        logger.warning("pdftotext failed on %s page %d: %s", path, page, exc)
        return ""
    return result.stdout


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
            text = _page_text(path, page).strip()
            if not text:
                continue
            total_chars += len(text)
            blocks.append(TextBlock(text=text, kind="page", extra={"page": page}))

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
