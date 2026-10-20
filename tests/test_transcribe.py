"""PDF pages read by the vision model (Phase 2b, ADR-030).

What matters here, in order: a page with no text layer reaches the index; a
transcription that invents a number does not; the model is never paid twice
for the same page; a sensitive reading is neither cached nor embedded; and the
vault side never transcribes where it could not cache.
"""

from __future__ import annotations

import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image, ImageDraw

from app.config import settings
from app.domains import Tier
from app.pipeline import state, vlm
from app.pipeline.extract import pdf, transcribe
from app.pipeline.extract.base import EMPTY, OCR_REQUIRED, OK

needs_poppler = pytest.mark.skipif(
    not (shutil.which("pdftoppm") and shutil.which("pdftotext")), reason="poppler-utils not installed"
)

TABLE_PAGE = "# Wiring\n\n| Pin | Torque |\n|---|---|\n| A | 12 Nm |\n| B | 15 Nm |"
LAYER = "Mounting the pump\nTighten the four bolts to 12 Nm, then the cover to 15 Nm.\n" * 3


# --- the pure parts -------------------------------------------------------


def test_page_specs_parse_and_print_back():
    assert transcribe.parse_pages("3,5-7") == ({3, 5, 6, 7}, False)
    assert transcribe.parse_pages(" ALL ") == (set(), True)
    assert transcribe.format_pages({3, 5, 6, 7, 9}, False) == "3,5-7,9"
    assert transcribe.format_pages(set(), True) == "all"
    for bad in ("", "0", "7-5", "x", "3,,a"):
        with pytest.raises(ValueError):
            transcribe.parse_pages(bad)


def test_a_number_the_text_layer_lacks_is_unsupported():
    layer = "Tighten to 12 Nm. Capacity 1500 L. Ratio 2.5:1"
    assert transcribe.unsupported_numbers("12 Nm, 2.5:1", layer) == []
    # The same digits printed with a thousands separator still count.
    assert transcribe.unsupported_numbers("1,500 L", layer) == []
    assert transcribe.unsupported_numbers("21 Nm and 12 Nm and 21 Nm", layer) == ["21"]


def test_markdown_headings_become_lines_and_levels():
    text, headings = transcribe.markdown_headings("# Setup\nbody\n## Wiring ##\n| a |")
    assert text == "Setup\nbody\nWiring\n| a |"
    assert headings == [("Setup", 1), ("Wiring", 2)]


def test_a_page_left_blank_on_purpose_is_not_a_scan():
    assert transcribe.lacks_text("", 100)
    assert transcribe.lacks_text("Fig. 3", 100)
    assert not transcribe.lacks_text("This page intentionally left blank.", 100)


# --- the extractor, with the model faked ------------------------------------


@pytest.fixture
def manual(tmp_path, monkeypatch):
    """A PDF whose pages' text layers are whatever the test says."""
    layers: dict[int, str] = {}

    def setup(*pages: str):
        layers.clear()
        layers.update({n: text for n, text in enumerate(pages, start=1)})
        monkeypatch.setattr(pdf, "_pdfinfo", lambda path: (len(layers), "Pump manual"))
        monkeypatch.setattr(pdf, "_page_text", lambda path, page, mode=pdf.MODE_RAW: layers[page])
        monkeypatch.setattr(pdf, "_headings", lambda path: {})
        path = tmp_path / "pump.pdf"
        path.write_bytes(b"%PDF-1.4")
        return path

    return setup


@pytest.fixture
def model(monkeypatch):
    """The vision model: answers from a list, records every page it was shown."""

    class Model:
        def __init__(self):
            self.answers: list = []
            self.calls = 0
            self.blank = False

        def __call__(self, *, prompt, images, max_tokens=None):
            assert prompt == transcribe.PROMPT and len(images) == 1
            self.calls += 1
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

    m = Model()
    monkeypatch.setattr(transcribe.vlm, "read_text", m)

    def render(path, page):
        image = Image.new("RGB", (200, 260), "white")
        if not m.blank:
            ImageDraw.Draw(image).rectangle((20, 20, 180, 60), fill="black")
        return image

    monkeypatch.setattr(transcribe, "render_page", render)
    return m


def _plan(**kw):
    return transcribe.PagePlan(file_hash="h", **kw)


def test_a_page_without_text_is_read_by_the_model(manual, model):
    model.answers = [TABLE_PAGE]
    plan = _plan()
    doc = pdf.PdfExtractor().extract(manual(LAYER, ""), plan=plan)

    assert model.calls == 1, "only the page without a text layer goes to the model"
    assert doc.status == OK
    read = [b for b in doc.blocks if b.extra["page"] == 2]
    assert [b.extra["extract_mode"] for b in read] == ["vlm"]
    assert read[0].extra["heading_path"] == ["Wiring"]
    assert "| A | 12 Nm |" in read[0].text
    assert plan.produced[2].status == transcribe.USED
    assert doc.extra["transcription"] == {"pages": {2: "used"}, "deferred": 0}


def test_without_a_plan_nothing_is_sent(manual, model):
    model.answers = [TABLE_PAGE]
    doc = pdf.PdfExtractor().extract(manual(LAYER, ""))
    assert model.calls == 0
    assert all(b.extra["extract_mode"] == "raw" for b in doc.blocks)


def test_an_invented_number_keeps_the_text_layer(manual, model):
    """The model says 21 Nm where the page says 12: the page keeps pdftotext."""
    model.answers = ["Tighten the four bolts to 21 Nm, then the cover to 15 Nm."]
    plan = _plan(requested={1})
    doc = pdf.PdfExtractor().extract(manual(LAYER), plan=plan)

    assert model.calls == 1
    assert doc.blocks[0].extra["extract_mode"] == "raw"
    assert "12 Nm" in doc.blocks[0].text
    assert plan.produced[1].status == transcribe.REJECTED
    assert plan.produced[1].text == "", "a rejected reading keeps no text to leak into the cache"
    assert "21" in plan.produced[1].detail


def test_a_requested_page_whose_numbers_check_out_replaces_the_layer(manual, model):
    model.answers = ["| Bolt | Torque |\n|---|---|\n| four bolts | 12 Nm |\n| cover | 15 Nm |"]
    doc = pdf.PdfExtractor().extract(manual(LAYER), plan=_plan(requested={1}))
    assert doc.blocks[0].extra["extract_mode"] == "vlm"
    assert "| cover | 15 Nm |" in doc.blocks[0].text


def test_a_cached_reading_costs_no_model_call(manual, model):
    plan = _plan(cached={2: transcribe.Transcription(transcribe.USED, TABLE_PAGE)})
    doc = pdf.PdfExtractor().extract(manual(LAYER, ""), plan=plan)
    assert model.calls == 0
    assert plan.produced == {}
    assert "| B | 15 Nm |" in doc.blocks[-1].text


def test_too_many_unread_pages_wait_for_a_human(manual, model):
    model.answers = [TABLE_PAGE]
    doc = pdf.PdfExtractor().extract(manual("", "", ""), plan=_plan(max_auto_pages=2))
    assert model.calls == 0
    assert doc.status == OCR_REQUIRED
    assert "rag transcribe URI=pump.pdf PAGES=all" in doc.status_detail
    assert doc.extra["transcription"]["deferred"] == 3


def test_requested_pages_ignore_the_automatic_limit(manual, model):
    model.answers = [TABLE_PAGE]
    doc = pdf.PdfExtractor().extract(manual("", "", ""), plan=_plan(requested_all=True, max_auto_pages=0))
    assert model.calls == 3
    assert doc.status == OK


def test_an_unreachable_model_is_tried_once_and_not_cached(manual, model):
    model.answers = [vlm.VisionError("cannot reach llama-server")]
    plan = _plan()
    doc = pdf.PdfExtractor().extract(manual("", "", ""), plan=plan)
    assert model.calls == 1, "after one failure the rest of the document is not attempted"
    assert plan.produced == {}, "a failure is retried next run, so it is not cached"
    assert doc.status == OCR_REQUIRED
    assert set(doc.extra["transcription"]["pages"].values()) == {"failed"}


def test_a_fenced_answer_is_unwrapped(manual, model):
    model.answers = ["```markdown\n" + TABLE_PAGE + "\n```"]
    doc = pdf.PdfExtractor().extract(manual(LAYER, ""), plan=_plan())
    assert doc.blocks[-1].text.startswith("Wiring")
    assert "```" not in doc.blocks[-1].text


def test_figure_markers_are_not_page_text(manual, model):
    """Qwen3-VL marks figures with their bounding boxes. Seen on the IntelliFlo
    spec page: the coordinates rejected an otherwise exact reading."""
    model.answers = [
        "Tighten the four bolts to 12 Nm, then the cover to 15 Nm.\n\n"
        "<!-- Image (54, 284, 924, 465) -->\n\n\n\nPump cover"
    ]
    plan = _plan(requested={1})
    doc = pdf.PdfExtractor().extract(manual(LAYER), plan=plan)
    assert plan.produced[1].status == transcribe.USED
    assert doc.blocks[0].text == "Tighten the four bolts to 12 Nm, then the cover to 15 Nm.\n\nPump cover"


def test_a_reading_that_never_ends_is_rejected_and_not_retried(manual, model):
    """The first real page looped on blank lines until the context filled.
    It is cached as rejected: the same page would loop the same way next run."""
    model.answers = [vlm.VisionTruncated("still answering at 4096 tokens")]
    plan = _plan()
    doc = pdf.PdfExtractor().extract(manual(LAYER, "", ""), plan=plan)
    assert model.calls == 2, "a loop is the page's fault, not the server's: the next page still goes"
    assert {r.status for r in plan.produced.values()} == {transcribe.REJECTED}
    assert "4096" in plan.produced[2].detail


def test_a_blank_page_is_not_sent(manual, model):
    model.blank = True
    plan = _plan()
    doc = pdf.PdfExtractor().extract(manual(LAYER, ""), plan=plan)
    assert model.calls == 0
    assert plan.produced[2].status == transcribe.BLANK
    assert doc.status == OK


def test_a_scan_read_in_full_is_indexed_however_short(manual, model):
    """Once every page has been read, a thin document is thin, not unread --
    calling it ocr_required would buy its pages again on every run."""
    model.answers = ["Fig. 1 Pump"]
    doc = pdf.PdfExtractor().extract(manual("", ""), plan=_plan())
    assert doc.status == OK


def test_a_scan_with_nothing_on_it_is_empty_not_ocr_required(manual, model):
    model.blank = True
    doc = pdf.PdfExtractor().extract(manual("", ""), plan=_plan())
    assert doc.status == EMPTY


# --- real PDFs, real pdftoppm ----------------------------------------------


def _scan(path, *pages_drawn: bool):
    """An image-only PDF: no text layer, as a scanner produces."""
    pages = []
    for drawn in pages_drawn:
        page = Image.new("RGB", (850, 1100), "white")
        if drawn:
            draw = ImageDraw.Draw(page)
            for y in range(100, 900, 60):
                draw.rectangle((100, y, 700, y + 20), fill="black")
        pages.append(page)
    pages[0].save(path, format="PDF", save_all=True, append_images=pages[1:])
    return path


@needs_poppler
def test_a_real_scan_is_rendered_and_read(tmp_path, monkeypatch):
    seen = []

    def read_text(*, prompt, images, max_tokens=None):
        seen.append(images[0][:2])
        return TABLE_PAGE

    monkeypatch.setattr(transcribe.vlm, "read_text", read_text)
    plan = _plan()
    doc = pdf.PdfExtractor().extract(_scan(tmp_path / "scan.pdf", True, False), plan=plan)

    assert seen == [b"\xff\xd8"], "one JPEG, for the page with ink on it"
    assert plan.produced[2].status == transcribe.BLANK
    assert doc.status == OK
    assert doc.blocks[0].extra["page"] == 1 and "| A | 12 Nm |" in doc.blocks[0].text


# --- the open tier: cache, scan, retraction ----------------------------------


@pytest.fixture
def open_source(tmp_path, monkeypatch):
    """An open-tier source with Qdrant and the embedder stubbed out."""
    from app.pipeline import embedder, qdrant_store
    from app.sources import LOCAL, Source

    monkeypatch.setattr(settings, "state_db_path", tmp_path / "rag.db")
    monkeypatch.setattr(settings, "quarantine_path", tmp_path / "quarantine")
    state.init()
    upserted = []
    monkeypatch.setattr(qdrant_store, "existing_chunks", lambda doc_id: {})
    monkeypatch.setattr(qdrant_store, "upsert_chunks", lambda points: upserted.extend(points))
    monkeypatch.setattr(qdrant_store, "make_point", lambda **kw: kw)
    monkeypatch.setattr(qdrant_store, "delete_doc_tail", lambda doc_id, n: None)
    monkeypatch.setattr(qdrant_store, "touch_doc", lambda doc_id, run_id: None)
    monkeypatch.setattr(qdrant_store, "sweep_source", lambda *a, **kw: 0)
    monkeypatch.setattr(embedder, "embed_passages", lambda texts: [[0.0]] * len(texts))
    monkeypatch.setattr(embedder, "sparse_passages", lambda texts: [None] * len(texts))
    docs = tmp_path / "manuals"
    docs.mkdir()
    source = Source(id="manuals-dir", type=LOCAL, domain="manuals", path=docs)
    source.upserted = upserted
    return source


def _readings(doc_id="all"):
    with state.reader() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM page_transcriptions ORDER BY page")]


@needs_poppler
def test_the_open_tier_reads_a_scan_once(open_source, monkeypatch):
    from app.pipeline import sync

    calls = []
    monkeypatch.setattr(transcribe.vlm, "read_text", lambda **kw: calls.append(1) or TABLE_PAGE)
    _scan(open_source.path / "scan.pdf", True)

    first = sync.sync_source(open_source, "run-1")
    assert (first.docs_indexed, first.pages_transcribed, len(calls)) == (1, 1, 1)
    assert "| A | 12 Nm |" in open_source.upserted[0]["payload"]["content"]
    cached = _readings()
    assert [(r["page"], r["status"], r["model"]) for r in cached] == [(1, "used", settings.vision_model)]

    second = sync.sync_source(open_source, "run-2")
    assert len(calls) == 1, "the second run used the cache"
    assert (second.docs_skipped, second.pages_transcribed) == (1, 0)


@needs_poppler
def test_a_sensitive_reading_is_quarantined_and_not_cached(open_source, monkeypatch):
    from app.pipeline import sync

    monkeypatch.setattr(transcribe.vlm, "read_text", lambda **kw: "Card 4111 1111 1111 1111 exp 09/29")
    _scan(open_source.path / "scan.pdf", True)

    result = sync.sync_source(open_source, "run-1")
    assert result.quarantined == 1
    assert open_source.upserted == [], "nothing reached the embedder"
    assert _readings() == []


def test_retraction_drops_readings_but_keeps_the_request(open_source, monkeypatch):
    from app import ingest
    from app.pipeline import qdrant_store

    monkeypatch.setattr(qdrant_store, "delete_doc", lambda doc_id: None)
    with state.writer() as conn:
        state.upsert_document(
            conn, doc_id="d1", source_id=open_source.id, domain="manuals", tier=Tier.OPEN.value,
            uri="pump.pdf", title="pump", content_hash="h", chunk_count=3, extractor="pdftotext",
            status=state.INDEXED, last_seen_run="r1",
        )
        state.save_transcriptions(conn, "d1", "f", {4: ("used", "text", None)}, model="m", prompt_version=1)
        state.set_transcription_request(conn, "d1", "4")
        ingest._apply(conn, state.get_document(conn, "d1"), "retracted", reason="wrong")
        assert state.transcriptions(conn, "d1", "f") == []
        assert state.transcription_request(conn, "d1") == "4"

        state.delete_document(conn, "d1")
        assert state.transcription_request(conn, "d1") is None


def test_a_reading_is_valid_only_for_the_file_it_was_made_from(open_source):
    with state.writer() as conn:
        state.save_transcriptions(conn, "d1", "old", {2: ("used", "text", None)}, model="m", prompt_version=1)
        assert state.transcriptions(conn, "d1", "new") == []
        assert [r["page"] for r in state.transcriptions(conn, "d1", "old")] == [2]


def test_the_transcribe_command_records_pages_and_forgets_their_readings(open_source, monkeypatch):
    from app import ingest

    ran = []
    monkeypatch.setattr(ingest, "all_sources", lambda: [open_source])
    monkeypatch.setattr(ingest, "_run", lambda sources, force, dry_run: ran.append(sources) or 0)
    with state.writer() as conn:
        state.upsert_document(
            conn, doc_id="d1", source_id=open_source.id, domain="manuals", tier=Tier.OPEN.value,
            uri="manuals/pump.pdf", title="pump", content_hash="h", chunk_count=3,
            extractor="pdftotext", status=state.INDEXED, last_seen_run="r1",
        )
        state.save_transcriptions(
            conn, "d1", "f", {5: ("rejected", "", "1 number"), 9: ("used", "x", None)},
            model="m", prompt_version=1,
        )

    assert ingest.main(["transcribe", "pump.pdf", "--pages", "7,5-6"]) == 0
    with state.reader() as conn:
        assert state.transcription_request(conn, "d1") == "5-7"
        assert [r["page"] for r in state.transcriptions(conn, "d1", "f")] == [9]
    assert ran == [[open_source]]

    assert ingest.main(["transcribe", "pump.pdf", "--clear"]) == 0
    with state.reader() as conn:
        assert state.transcription_request(conn, "d1") is None

    with pytest.raises(SystemExit, match="PAGES"):
        ingest.main(["transcribe", "pump.pdf", "--pages", "nine"])


# --- the vault: inbox only ---------------------------------------------------


class _StubVision:
    """llama-server's chat endpoint, answering every request with one reading.
    Over HTTP because the parse worker is a separate interpreter."""

    def __init__(self, markdown, finish_reason="stop"):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                stub.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = json.dumps(
                    {"choices": [{"message": {"content": markdown}, "finish_reason": finish_reason}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def vault_scan(unlocked, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db_path", tmp_path / "state.db")
    monkeypatch.setattr(settings, "data_root", tmp_path)
    state.init()
    stub = _StubVision("# Statement\n\nClosing balance 1,204.50 on 30 September.")
    monkeypatch.setenv("LLAMA_URL", stub.url)
    inbox = tmp_path / "inbox" / "financial"
    inbox.mkdir(parents=True)
    _scan(inbox / "statement.pdf", True)
    yield inbox, stub
    stub.server.shutdown()


def _vault_chunks():
    from app.vault.keyagent import AGENT

    conn = AGENT.connect()
    try:
        return [r["text"] for r in conn.execute("SELECT text FROM chunks")]
    finally:
        conn.close()


@needs_poppler
def test_a_scan_dropped_in_a_vault_inbox_is_read(vault_scan):
    from app.pipeline import sync
    from app.sources import INBOX, Source

    inbox, stub = vault_scan
    result = sync.sync_source(Source(id="inbox:financial", type=INBOX, domain="financial", path=inbox), "run-1")

    assert len(stub.requests) == 1
    assert (result.docs_indexed, result.pages_transcribed) == (1, 1)
    assert any("Closing balance 1,204.50" in text for text in _vault_chunks())


@needs_poppler
def test_a_vault_source_indexed_in_place_is_never_transcribed(vault_scan):
    """No cache is possible there, so every run would buy every page again."""
    from app.pipeline import sync
    from app.sources import LOCAL, Source

    inbox, stub = vault_scan
    result = sync.sync_source(Source(id="fin-dir", type=LOCAL, domain="financial", path=inbox), "run-1")

    assert stub.requests == []
    assert result.ocr_required == 1
    assert _vault_chunks() == []


def test_every_vision_request_is_capped_by_the_server(monkeypatch):
    """llama-server's router keeps generating after a client gives up, so the
    cap must travel with the request; hitting it is reported, not returned."""
    stub = _StubVision("| a |\n\n\n\n", finish_reason="length")
    monkeypatch.setattr(settings, "llama_url", stub.url)
    try:
        with pytest.raises(vlm.VisionTruncated):
            vlm.read_text(prompt="p", images=[b"x"])
        with pytest.raises(vlm.VisionTruncated):
            vlm.read_json(prompt="p", schema={"type": "object"}, text="receipt")
    finally:
        stub.server.shutdown()
    assert [r["max_tokens"] for r in stub.requests] == [settings.vision_max_tokens] * 2
    assert "response_format" not in stub.requests[0], "a page is asked for as plain text"
    assert "response_format" in stub.requests[1], "a receipt is still schema-constrained"
