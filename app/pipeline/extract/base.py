"""Extractor contract.

Extractors turn a file into text. They do NOT decide tier, do not embed, and do
not touch a store -- that ordering (extract -> scan -> route -> embed) is what
keeps a misfiled sensitive document from reaching Qdrant.

Extractor choice is driven by FILE TYPE, not by sensitivity: a text-native PDF
uses pdftotext whether it is a pool-pump manual or a bank statement, and a photo
uses the vision model either way.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.pipeline.chunker import TextBlock

# Chunking strategies a document can ask for.
MARKDOWN = "markdown"
TEXT = "text"
BLOCKS = "blocks"

# Statuses an extractor can report.
OK = "ok"
OCR_REQUIRED = "ocr_required"
EXTRACT_FAILED = "extract_failed"
EMPTY = "empty"


@dataclass
class RawDoc:
    rel_uri: str
    strategy: str
    title: str | None = None
    text: str = ""
    blocks: list[TextBlock] = field(default_factory=list)
    mtime: float = 0.0
    size_bytes: int = 0
    extractor: str = ""
    status: str = OK
    status_detail: str | None = None
    extra: dict = field(default_factory=dict)

    def full_text(self) -> str:
        """Everything the document contributes, for hashing and scanning.

        The sensitivity scan runs against this, so it must include block
        content too -- scanning only `text` would miss every block-structured
        document.
        """
        if self.strategy == BLOCKS:
            return "\n\n".join(b.text for b in self.blocks)
        return self.text

    def content_hash(self) -> str:
        return hashlib.sha256(self.full_text().encode("utf-8", "replace")).hexdigest()

    @property
    def usable(self) -> bool:
        return self.status == OK and bool(self.full_text().strip())


@runtime_checkable
class Extractor(Protocol):
    name: str

    def matches(self, path: Path) -> bool: ...

    def extract(self, path: Path) -> RawDoc: ...


def doc_id_for(source_id: str, rel_uri: str) -> str:
    """Stable document identity.

    Hashes the URI *relative to the source root*, not an absolute container
    path, so changing a bind mount or moving the stack to another host does not
    orphan every document in the index.
    """
    return hashlib.sha256(f"{source_id}\x00{rel_uri}".encode()).hexdigest()[:32]


def is_binary(path: Path, probe: int = 2048) -> bool:
    try:
        return b"\x00" in path.open("rb").read(probe)
    except OSError:
        return True
