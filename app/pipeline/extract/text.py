"""Plain text and markdown."""

from __future__ import annotations

import re
from pathlib import Path

from app.pipeline.extract.base import EMPTY, MARKDOWN, OK, TEXT, RawDoc, is_binary

MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdown"}
TEXT_SUFFIXES = {".txt", ".rst", ".log", ".text"}

MAX_BYTES = 5 * 1024 * 1024

_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


class TextExtractor:
    name = "text"

    def matches(self, path: Path) -> bool:
        return path.suffix.lower() in (MARKDOWN_SUFFIXES | TEXT_SUFFIXES)

    def extract(self, path: Path) -> RawDoc:
        stat = path.stat()
        is_markdown = path.suffix.lower() in MARKDOWN_SUFFIXES
        doc = RawDoc(
            rel_uri=path.name,
            strategy=MARKDOWN if is_markdown else TEXT,
            mtime=stat.st_mtime,
            size_bytes=stat.st_size,
            extractor="markdown" if is_markdown else "text",
        )

        if stat.st_size > MAX_BYTES:
            doc.status = EMPTY
            doc.status_detail = f"{stat.st_size} bytes exceeds the {MAX_BYTES}-byte limit"
            return doc
        if is_binary(path):
            doc.status = EMPTY
            doc.status_detail = "binary content"
            return doc

        body = path.read_text(encoding="utf-8", errors="replace").strip()
        if not body:
            doc.status = EMPTY
            doc.status_detail = "no text"
            return doc

        doc.text = body
        doc.status = OK
        heading = _H1_RE.search(body) if is_markdown else None
        doc.title = heading.group(1).strip() if heading else path.stem
        return doc
