"""PDF pages read by the vision model, for pages `pdftotext` cannot serve.

Two kinds of page come here (ADR-030):

- Pages with no usable text layer -- a scan, or a scanned insert in an
  otherwise digital manual -- automatically, up to
  `settings.transcribe_max_auto_pages` per document. Past that the document
  waits for a human: a scanned book is a day of CPU nobody chose.
- Pages a human named with `rag transcribe`: whitespace-aligned tables and
  anything else the text layer mangles. Nothing detects those automatically;
  ADR-024 found no threshold that separates them from two-column prose.

The model is asked for markdown, so a table comes back as a pipe table and the
table-aware chunker repeats its header across chunks.

**The model copies; Python checks.** On a page that has a text layer, every
number in the transcription must also appear in that layer, or the
transcription is rejected and the page keeps its `pdftotext` text. A misread
torque figure that reached the index would be cited with full confidence. A
page with no text layer has nothing to check against, which is the residual
risk of transcribing scans at all, and the reason citations stay verbatim.

Runs wherever extraction runs, including the keyless parse worker, so it must
not import anything key-bearing.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageStat

from app.config import settings
from app.pipeline import vlm

logger = logging.getLogger(__name__)

# Bump when PROMPT changes in a way that should be visible in the cache. It is
# recorded, not acted on: re-transcribing a corpus costs hours, so a better
# prompt reaches cached pages through `rag transcribe`, never silently.
PROMPT_VERSION = 1

PROMPT = """You transcribe one page of a PDF document into GitHub-flavoured markdown so that it can be searched.

- Copy the text exactly as printed: the same words, numbers, units and symbols. Never summarise, translate, correct, complete or reorder it.
- A table becomes a markdown pipe table: its header row first, then one row per printed row, with empty cells left empty.
- A heading becomes a # line, one # per level of heading.
- Numbered steps keep their numbers exactly as printed.
- Leave out running headers, running footers and the page number.
- For a picture, transcribe only text printed inside it. Never describe it.
- Answer with the markdown only, no code fence and no commentary. A page with no text gives an empty answer."""

_FENCE_RE = re.compile(r"^```[a-z]*\n(.*?)\n?```$", re.DOTALL)
# Qwen3-VL marks each figure with `<!-- Image (54, 284, 924, 465) -->`, its
# bounding box, whatever the prompt says. Those coordinates are not page text:
# indexed, they are junk numbers; checked, they reject an accurate reading.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


# Outcomes. Everything but FAILED is cached: a failure (server down, timeout)
# is retried on the next run, and anything else would cost the same minute of
# CPU to learn again.
USED = "used"
REJECTED = "rejected"
BLANK = "blank"
FAILED = "failed"

# A rendered page this flat is blank paper, and is not worth a model call.
_BLANK_STDDEV = 2.0
# "This page intentionally left blank" has a text layer of a few dozen
# characters, so it looks like a scan to the per-page check.
_LEFT_BLANK_RE = re.compile(r"intentionally\s+(?:left\s+)?blank", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# How many unmatched numbers a rejection names.
_DETAIL_NUMBERS = 5


@dataclass
class Transcription:
    status: str
    text: str = ""
    detail: str | None = None


@dataclass
class PagePlan:
    """Which pages of one PDF go to the model, and what is already known.

    Built by the caller, which knows the document's identity and where its
    cache lives; the extractor only sees a file. `produced` comes back filled
    with the transcriptions made this run, for the caller to store once the
    document has passed the sensitivity scan.
    """

    file_hash: str
    # Pages a human asked for; `requested_all` is `rag transcribe PAGES=all`.
    requested: set[int] = field(default_factory=set)
    requested_all: bool = False
    # Transcribe pages without a text layer unprompted.
    auto: bool = True
    max_auto_pages: int = field(default_factory=lambda: settings.transcribe_max_auto_pages)
    cached: dict[int, Transcription] = field(default_factory=dict)
    produced: dict[int, Transcription] = field(default_factory=dict)

    def is_requested(self, page: int) -> bool:
        return self.requested_all or page in self.requested


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def lacks_text(text: str, min_chars: int) -> bool:
    """Whether a page's text layer is too thin to be the page."""
    stripped = text.strip()
    return len(stripped) < min_chars and not _LEFT_BLANK_RE.search(stripped)


def parse_pages(spec: str) -> tuple[set[int], bool]:
    """`3,5-7` -> ({3, 5, 6, 7}, False); `all` -> (set(), True)."""
    spec = spec.strip().lower()
    if spec == "all":
        return set(), True
    pages: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        first, _, last = part.partition("-")
        try:
            lo, hi = int(first), int(last or first)
        except ValueError:
            raise ValueError(f"not a page or range: {part!r}") from None
        if lo < 1 or hi < lo:
            raise ValueError(f"not a page or range: {part!r}")
        pages.update(range(lo, hi + 1))
    if not pages:
        raise ValueError("no pages given")
    return pages, False


def format_pages(pages: set[int], all_pages: bool) -> str:
    """The inverse of parse_pages, as stored and shown."""
    if all_pages:
        return "all"
    ordered, runs = sorted(pages), []
    for page in ordered:
        if runs and page == runs[-1][1] + 1:
            runs[-1][1] = page
        else:
            runs.append([page, page])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def unsupported_numbers(markdown: str, layer: str) -> list[str]:
    """Numbers in the transcription that the page's text layer does not have.

    Conservative on purpose: `1,500` against a layer reading `1 500` counts as
    unsupported, which costs a page its transcription, never the index a
    wrong figure.
    """
    have = set(_NUMBER_RE.findall(layer))
    have_digits = {re.sub(r"\D", "", n) for n in have}
    missing: list[str] = []
    for number in _NUMBER_RE.findall(markdown):
        if number in have or re.sub(r"\D", "", number) in have_digits:
            continue
        if number not in missing:
            missing.append(number)
    return missing


def markdown_headings(markdown: str) -> tuple[str, list[tuple[str, int]]]:
    """Strip `#` markers and return (text, [(heading, level)]) in page order.

    The section splitter matches heading LINES, as it does for headings found
    by font size, so the markers go and the levels travel separately. Markdown
    level 1 lines up with the largest font size, which is what the extractor
    calls level 1 too.
    """
    lines, found = [], []
    for line in markdown.splitlines():
        match = _MD_HEADING_RE.match(line)
        if match and match.group(2):
            found.append((match.group(2), len(match.group(1))))
            lines.append(match.group(2))
        else:
            lines.append(line)
    return "\n".join(lines), found


def render_page(path: Path, page: int) -> Image.Image:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["pdftoppm", "-jpeg", "-scale-to", str(settings.transcribe_image_max_px),
             "-f", str(page), "-l", str(page), "-singlefile", str(path), f"{tmp}/page"],
            capture_output=True, timeout=300, check=True,
        )
        with Image.open(f"{tmp}/page.jpg") as image:
            return image.convert("RGB")


def _is_blank(image: Image.Image) -> bool:
    return ImageStat.Stat(image.convert("L")).stddev[0] < _BLANK_STDDEV


def clean(answer: str) -> str:
    """The page's markdown: no ```markdown fence, no figure markers, and no
    run of blank lines where a marker stood."""
    answer = answer.strip()
    match = _FENCE_RE.match(answer)
    answer = match.group(1) if match else answer
    answer = _COMMENT_RE.sub("", answer)
    return re.sub(r"\n{3,}", "\n\n", answer).strip()


def _jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def transcribe(path: Path, page: int, layer: str, *, check: bool) -> Transcription:
    """Read one page with the vision model.

    `layer` is the page's own text (both `pdftotext` orderings); with `check`
    every number transcribed must appear in it. Raises vlm.VisionError when
    the model cannot be reached, which the caller turns into FAILED.
    """
    image = render_page(path, page)
    if _is_blank(image):
        return Transcription(BLANK)
    try:
        markdown = clean(vlm.read_text(prompt=PROMPT, images=[_jpeg(image)]))
    except vlm.VisionTruncated as exc:
        # Cached like a rejection: the same page would loop the same way on
        # every run. `rag transcribe` asks again after a prompt or model change.
        return Transcription(REJECTED, detail=f"no complete reading: {exc}")
    if not markdown:
        return Transcription(BLANK)
    if check:
        missing = unsupported_numbers(markdown, layer)
        if missing:
            shown = ", ".join(missing[:_DETAIL_NUMBERS]) + (", ..." if len(missing) > _DETAIL_NUMBERS else "")
            return Transcription(
                REJECTED, detail=f"{len(missing)} number(s) not in the text layer: {shown}"
            )
    return Transcription(USED, text=markdown)
