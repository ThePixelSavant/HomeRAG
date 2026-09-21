"""Extractor registry, dispatched by file type."""

from __future__ import annotations

from pathlib import Path

from app.pipeline.extract.base import (
    BLOCKS,
    EMPTY,
    EXTRACT_FAILED,
    MARKDOWN,
    OCR_REQUIRED,
    OK,
    TEXT,
    Extractor,
    RawDoc,
    chunks_for,
    doc_id_for,
)
from app.pipeline.extract.pdf import PdfExtractor
from app.pipeline.extract.text import TextExtractor
from app.pipeline.extract.transcript import TranscriptExtractor

# Order matters only where suffixes overlap, which today they do not.
_REGISTRY: list[Extractor] = [TextExtractor(), PdfExtractor(), TranscriptExtractor()]


def register(extractor: Extractor) -> None:
    _REGISTRY.append(extractor)


def dispatch(path: Path) -> Extractor | None:
    for extractor in _REGISTRY:
        if extractor.matches(path):
            return extractor
    return None


def extract(path: Path) -> RawDoc | None:
    extractor = dispatch(path)
    if extractor is None:
        return None
    try:
        return extractor.extract(path)
    except Exception as exc:  # noqa: BLE001 - one bad file must not kill a run
        return RawDoc(
            rel_uri=path.name,
            strategy=TEXT,
            extractor=getattr(extractor, "name", "unknown"),
            status=EXTRACT_FAILED,
            status_detail=f"{type(exc).__name__}: {exc}",
        )


__all__ = [
    "BLOCKS",
    "EMPTY",
    "EXTRACT_FAILED",
    "MARKDOWN",
    "OCR_REQUIRED",
    "OK",
    "TEXT",
    "Extractor",
    "RawDoc",
    "PdfExtractor",
    "TextExtractor",
    "TranscriptExtractor",
    "dispatch",
    "doc_id_for",
    "extract",
    "register",
]
