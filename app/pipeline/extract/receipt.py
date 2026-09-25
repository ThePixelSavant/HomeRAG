"""Receipts, read by the vision model.

Dispatched by DOMAIN, not by suffix (see parse_worker): a PDF filed under
`manuals` is a manual and goes to PdfExtractor; the same PDF under `receipts`
is a transaction and comes here. Runs only in the keyless parse worker.

Three inputs, cheapest first:

- A PDF with a text layer (an emailed e-receipt) is sent as TEXT. A page of
  text is a few hundred tokens; the same page as an image is ~1,500 plus the
  projector's encode, all prefilled on the CPU.
- A PDF without one (a scan) is rendered with `pdftoppm`, up to
  `receipt_max_pages`.
- A photo is EXIF-rotated and scaled to `receipt_image_max_px` on its long
  edge. Full size, a 12 MP phone photo would be ~15k image tokens.

The document's text is the reading rendered back to plain lines -- what
`search_vault` finds -- and the raw reading travels to the parent in
`extra["receipt"]`, where it is validated before it can become a ledger row.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from app import receipts
from app.config import settings
from app.pipeline import vlm
from app.pipeline.extract.base import EMPTY, OK, TEXT, RawDoc

# Below this many characters a PDF's text layer is not the receipt -- a scan
# with a stamped header, say -- and it is read as an image instead.
MIN_TEXT_CHARS = 100


def _jpeg(image: Image.Image) -> bytes:
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((settings.receipt_image_max_px, settings.receipt_image_max_px))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _text_layer(path: Path) -> str:
    # -layout keeps "Item ..... 4.99" on one line, which is how a receipt reads.
    result = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", "-l", str(settings.receipt_max_pages), str(path), "-"],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip()


def _render_pages(path: Path) -> list[bytes]:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["pdftoppm", "-jpeg", "-scale-to", str(settings.receipt_image_max_px),
             "-l", str(settings.receipt_max_pages), str(path), f"{tmp}/page"],
            capture_output=True, timeout=300, check=True,
        )
        return [_jpeg(Image.open(page)) for page in sorted(Path(tmp).glob("page*.jpg"))]


def extract_receipt(path: Path) -> RawDoc:
    stat = path.stat()
    doc = RawDoc(
        rel_uri=path.name,
        strategy=TEXT,
        title=path.stem,
        mtime=stat.st_mtime,
        size_bytes=stat.st_size,
        extractor=f"vlm:{settings.vision_model}",
    )

    text, images = "", []
    if path.suffix.lower() == ".pdf":
        text = _text_layer(path)
        if len(text) < MIN_TEXT_CHARS:
            text, images = "", _render_pages(path)
    else:
        with Image.open(path) as image:
            images = [_jpeg(image)]

    if not text and not images:
        doc.status, doc.status_detail = EMPTY, "no text layer and no pages rendered"
        return doc

    reading = vlm.read_json(
        prompt=receipts.PROMPT,
        schema=receipts.SCHEMA,
        text=f"Receipt text:\n{text}" if text else None,
        images=images,
    )
    doc.text = receipts.render_text(reading)
    doc.extra["receipt"] = reading
    doc.extra["receipt_input"] = "text" if text else f"{len(images)} image(s)"
    doc.status = OK
    return doc
