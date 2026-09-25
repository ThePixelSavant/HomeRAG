"""Pipeline invariants: chunking, identity, classification, tiering."""

from __future__ import annotations

import pytest

from app.config import settings
from app.domains import Tier, UnknownDomainError, tier_of
from app.pipeline import chunker, classify
from app.pipeline.chunker import TextBlock
from app.pipeline.extract.base import doc_id_for
from app.pipeline.qdrant_store import point_id

# Long enough to exceed the model ceiling, so the chunking tests exercise more
# than a single chunk and the tokenizer test proves truncation is disabled.
LOREM = "The pump must be primed before first use. " * 200


# --- chunking --------------------------------------------------------------


def test_every_chunk_fits_the_model_ceiling():
    """Over-budget text is truncated by the model with no error, so the
    chunker must never emit any."""
    for chunk in chunker.chunk_text(LOREM):
        assert chunk.token_count <= settings.chunk_max_tokens


def test_chunk_indices_are_contiguous():
    chunks = chunker.chunk_text(LOREM)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_empty_text_yields_nothing():
    assert chunker.chunk_text("") == []
    assert chunker.chunk_text("   \n  ") == []


def test_markdown_carries_the_heading_breadcrumb():
    md = "# Pump\n\n## Wiring\n\nTighten to 18 ft-lb in a star pattern.\n"
    chunk = chunker.chunk_markdown(md)[0]
    assert chunk.heading_path == ["Pump", "Wiring"]
    # The breadcrumb must be in the embedded text, not only in metadata:
    # a torque spec is meaningless without knowing the assembly.
    assert chunk.text.startswith("Pump > Wiring")


def test_blocks_are_not_split_when_they_fit():
    blocks = [TextBlock(text=f"Turn {i}. " + "word " * 20, kind="user") for i in range(4)]
    chunks = chunker.chunk_blocks(blocks)
    assert len(chunks) == 1
    for i in range(4):
        assert f"Turn {i}." in chunks[0].text


def test_oversized_block_is_split_and_still_fits():
    chunks = chunker.chunk_blocks([TextBlock(text="alpha beta gamma " * 500, kind="tool")])
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_count <= settings.chunk_max_tokens


def test_long_text_is_not_measured_through_a_truncating_tokenizer():
    """fastembed's own tokenizer truncates at the model max; counting with it
    would cap every measurement at 512 and hide the rest of the document."""
    from app.pipeline.embedder import MODEL_MAX_TOKENS, count_tokens

    assert count_tokens(LOREM) > MODEL_MAX_TOKENS


# --- identity --------------------------------------------------------------


def test_point_ids_are_deterministic():
    assert point_id("s", "d", 3) == point_id("s", "d", 3)


def test_point_ids_differ_by_position_and_document():
    assert point_id("s", "d", 3) != point_id("s", "d", 4)
    assert point_id("s", "d", 3) != point_id("s", "e", 3)


def test_doc_id_uses_a_source_relative_uri():
    """Hashing an absolute container path would orphan the whole index the
    first time a bind mount changed."""
    assert doc_id_for("src", "a/b.md") == doc_id_for("src", "a/b.md")
    assert doc_id_for("src", "a/b.md") != doc_id_for("other", "a/b.md")


# --- classification --------------------------------------------------------


def test_luhn_valid_card_is_flagged():
    assert classify.scan("paid with 4539 1488 0343 6467 today").sensitive


def test_non_luhn_long_number_is_not_flagged():
    """Order numbers must not flood the review queue, or it becomes noise
    people learn to wave through."""
    assert not classify.scan("order number 1234 5678 9012 3456").sensitive


@pytest.mark.parametrize(
    "text",
    [
        "SSN: 123-45-6789",
        "ANTHROPIC_API_KEY=sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
        "Statement. Opening balance $1,204. Account ending 4432.",
    ],
)
def test_sensitive_shapes_are_flagged(text):
    assert classify.scan(text).sensitive


def test_ordinary_prose_is_not_flagged():
    assert not classify.scan("I should budget better for tools this year.").sensitive


def test_findings_never_echo_the_live_secret():
    reason = classify.scan("key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx").reason()
    assert "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx" not in reason


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx",
        "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123",
        "AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_redaction_removes_secrets_before_embedding(secret):
    assert secret not in classify.redact(f"here it is: {secret} end")


# --- tiering ---------------------------------------------------------------


def test_known_domains_map_to_expected_tiers():
    assert tier_of("manuals") is Tier.OPEN
    assert tier_of("receipts") is Tier.VAULT
    assert tier_of("transcripts") is Tier.VAULT


def test_unknown_domain_raises_rather_than_defaulting():
    """Defaulting a typo would route sensitive data into the open tier, and a
    plaintext vector cannot be un-published."""
    with pytest.raises(UnknownDomainError):
        tier_of("reciepts")


# --- source manifest -------------------------------------------------------


def test_source_without_domain_is_rejected(tmp_path):
    import yaml

    from app.sources import SourceConfigError, load_sources

    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump({"sources": [{"id": "x", "type": "local", "path": "/docs"}]}))
    with pytest.raises(SourceConfigError, match="domain"):
        load_sources(path)


def test_inbox_directory_must_name_a_known_domain(tmp_path):
    from app.sources import SourceConfigError, inbox_sources

    (tmp_path / "reciepts").mkdir()
    with pytest.raises(SourceConfigError):
        inbox_sources(tmp_path)


def test_inbox_directory_name_sets_the_domain(tmp_path):
    from app.sources import inbox_sources

    (tmp_path / "receipts").mkdir()
    (tmp_path / "manuals").mkdir()
    found = {s.domain: s for s in inbox_sources(tmp_path)}
    assert found["receipts"].is_vault
    assert not found["manuals"].is_vault


# --- enumeration -----------------------------------------------------------


def test_files_directly_in_a_source_root_are_found(tmp_path):
    """Regression: fnmatch("a.md", "**/*") is False because "**/" needs a
    literal slash, which made every inbox drop invisible."""
    from app.pipeline.sync import enumerate_files
    from app.sources import Source

    (tmp_path / "pump.md").write_text("# Pump\n")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "other.md").write_text("# Other\n")

    files, complete = enumerate_files(
        Source(id="s", type="inbox", domain="manuals", path=tmp_path)
    )
    assert complete
    assert {f.name for f in files} == {"pump.md", "other.md"}


def test_missing_source_path_reports_incomplete_enumeration(tmp_path):
    """The sweep keys off this flag; an empty result from a broken mount is
    indistinguishable from a source that genuinely lost every file."""
    from app.pipeline.sync import enumerate_files
    from app.sources import Source

    files, complete = enumerate_files(
        Source(id="s", type="local", domain="notes", path=tmp_path / "nope")
    )
    assert files == []
    assert complete is False


def test_same_basename_in_different_directories_does_not_collide(tmp_path):
    """Regression: doc_id derives from rel_uri, and extractors default it to
    the basename because they only ever see one file. Without the source-root
    relative path, every CLAUDE.md in a tree collapses into one document, each
    silently overwriting the last."""
    from app.pipeline.extract.base import doc_id_for

    assert doc_id_for("dev-notes", "RAG/CLAUDE.md") != doc_id_for("dev-notes", "LLM/CLAUDE.md")


def test_sync_sets_the_source_relative_uri(tmp_path):
    from app.pipeline import extract
    from app.sources import Source

    (tmp_path / "RAG").mkdir()
    (tmp_path / "LLM").mkdir()
    (tmp_path / "RAG" / "CLAUDE.md").write_text("# RAG\n\nOne.\n")
    (tmp_path / "LLM" / "CLAUDE.md").write_text("# LLM\n\nTwo.\n")

    source = Source(id="s", type="local", domain="notes", path=tmp_path)
    from app.pipeline.sync import enumerate_files

    files, _ = enumerate_files(source)
    uris = set()
    for path in files:
        doc = extract.extract(path)
        doc.rel_uri = str(path.relative_to(source.path))
        uris.add(doc.rel_uri)
    assert uris == {"RAG/CLAUDE.md", "LLM/CLAUDE.md"}


# --- table-aware chunking --------------------------------------------------

TORQUE_TABLE = (
    "# Specs\n\n## Torque\n\nUse a calibrated wrench.\n\n"
    "| Fastener | Torque | Notes |\n|---|---|---|\n"
    + "\n".join(
        f"| Bolt M{n} | {n * 2} ft-lb | tighten in a cross pattern, re-check after warm-up |"
        for n in range(8, 60)
    )
    + "\n\nDone.\n"
)


def test_split_table_repeats_its_header_on_every_chunk():
    """A continuation chunk of `| Bolt M27 | 54 ft-lb |` with no column labels
    cannot tell the reader which number is the torque."""
    chunks = chunker.chunk_markdown(TORQUE_TABLE)
    assert len(chunks) > 1
    for chunk in chunks:
        assert "| Fastener | Torque | Notes |" in chunk.text


def test_repeated_header_is_charged_to_the_budget():
    for chunk in chunker.chunk_markdown(TORQUE_TABLE):
        assert chunk.token_count <= settings.chunk_max_tokens


def test_small_table_is_not_given_a_duplicate_header():
    chunks = chunker.chunk_markdown("| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert len(chunks) == 1
    assert chunks[0].text.count("| A | B |") == 1


def test_prose_containing_a_dash_is_not_treated_as_a_table():
    """A strict separator test matters: mistaking prose for a table would
    prepend a nonsense 'header' to every following chunk."""
    assert chunker.find_tables("Some text | with a pipe\n--- and a dash\nmore\n") == []


# --- locators --------------------------------------------------------------


def test_text_chunks_report_their_line_range():
    chunks = chunker.chunk_text(LOREM)
    lines = chunks[0].extra["locator"]["lines"]
    assert lines[0] == 1 and lines[1] >= lines[0]


def test_markdown_locator_lines_are_relative_to_the_whole_file():
    md = "# One\n\nAlpha.\n\n# Two\n\nBravo.\n"
    second = chunker.chunk_markdown(md)[1]
    assert second.heading_path == ["Two"]
    # "Bravo." is on line 7 of the document, not line 1 of its section.
    assert second.extra["locator"]["lines"] == [7, 7]


def test_chunk_spanning_two_pages_keeps_both():
    """dict.update keeps only the last block's page, so a chunk built from
    pages 11 and 12 would cite 12 -- and send the reader past the answer."""
    blocks = [
        TextBlock(text="First page text.", kind="page", extra={"page": 11}),
        TextBlock(text="Second page text.", kind="page", extra={"page": 12}),
    ]
    chunk = chunker.chunk_blocks(blocks)[0]
    assert chunk.extra["locator"] == {"page": 11, "pages": [11, 12]}


def test_citations_name_the_first_page_of_a_range():
    from app.documents import format_citation

    assert format_citation("pump.pdf", {"page": 12, "pages": [12]}) == "pump.pdf, page 12"
    assert format_citation("pump.pdf", {"page": 11, "pages": [11, 12]}) == "pump.pdf, pages 11-12"
    assert format_citation("net.md", {"lines": [40, 58]}) == "net.md, lines 40-58"
    assert format_citation("net.md", {"lines": [7, 7]}) == "net.md, line 7"
    assert format_citation("docs", {"anchor": "install"}) == "docs#install"
    assert format_citation("receipt.jpg", {}) == "receipt.jpg"


def _full_hit(**overrides):
    hit = {
        "score": 0.571428, "content": "MIDI channel: 1 (default)", "title": "TB-03",
        "uri": "tb03.pdf", "domain": "manuals", "source_id": "inbox:manuals",
        "doc_id": "d1", "chunk_index": 4, "heading_path": [], "indexed_at": "2026-09-24",
        "locator": {"page": 3, "pages": [3]}, "citation": "tb03.pdf, page 3",
        "lifecycle": "active", "superseded_by": None,
    }
    return {**hit, **overrides}


def test_model_view_keeps_what_the_model_uses():
    from app.documents import for_model

    assert for_model(_full_hit()) == {
        "content": "MIDI channel: 1 (default)",
        "citation": "tb03.pdf, page 3",
        "doc_id": "d1",
        "chunk_index": 4,
        "score": 0.571,
    }


def test_model_view_reports_lifecycle_only_when_it_is_news():
    from app.documents import for_model

    old = for_model(_full_hit(lifecycle="superseded", superseded_by="tb03-v2.pdf"))
    assert old["lifecycle"] == "superseded" and old["superseded_by"] == "tb03-v2.pdf"
    # The stale banner rides in the content, which is passed through untouched.
    stale = for_model(_full_hit(lifecycle="stale", content="[STALE: old]\nbody"))
    assert stale["lifecycle"] == "stale" and stale["content"].startswith("[STALE")


def test_fetch_context_caps_the_window(tmp_path, monkeypatch):
    from app import main
    from app.pipeline import qdrant_store

    monkeypatch.setattr(settings, "state_db_path", tmp_path / "absent.db")
    asked = {}
    monkeypatch.setattr(
        qdrant_store, "fetch_context", lambda doc_id, idx, **kw: asked.update(kw) or []
    )

    main.fetch_context("d1", 11, before=5, after=5)
    assert asked == {"before": 2, "after": 2}
    main.fetch_context("d1", 11)
    assert asked == {"before": 1, "after": 1}


def test_model_view_passes_errors_and_context_rows_through():
    from app.documents import for_model

    error = {"error": "Unknown domain 'manual'.", "valid_domains": ["manuals"]}
    assert for_model(error) is error
    # fetch_context rows have no score.
    assert "score" not in for_model(_full_hit(score=None))


# --- PDF extraction ----------------------------------------------------------


def test_respace_splits_runs_raw_glued_together():
    """`-raw` drops spaces in letter-spaced text; reading order has them."""
    from app.pipeline.extract.pdf import _respace

    reading = "INJURY OR DEATH. THIS PUMP SHOULD BE\nINSTALLED BY A PROFESSIONAL."
    raw = "INJURYORDEATH.THISPUMP SHOULD BE\nINSTALLED BY A PROFESSIONAL."
    assert _respace(raw, reading) == (
        "INJURY OR DEATH. THIS PUMP SHOULD BE\nINSTALLED BY A PROFESSIONAL."
    )


def test_respace_leaves_words_reading_order_also_produced():
    from app.pipeline.extract.pdf import _respace

    # "therapist" could be spelled "the rapist" from this vocabulary. It is
    # a word reading order produced, so it is never split.
    assert _respace("the therapist", "the rapist the therapist") == "the therapist"
    # Nothing in reading order spells it: left alone rather than guessed at.
    assert _respace("KEYTRANSPOSE", "unrelated words") == "KEYTRANSPOSE"


def test_respace_uses_the_fewest_pieces():
    from app.pipeline.extract.pdf import _respace

    assert _respace("1.Press MasterTune", "1. Press Master Tune MasterTune") == (
        "1. Press MasterTune"
    )


def test_pages_are_extracted_in_stored_order(tmp_path, monkeypatch):
    """Every page takes `-raw`, re-spaced from reading order, and says so."""
    from app.pipeline.extract import pdf

    calls = []

    def fake(path, page, mode=pdf.MODE_RAW):
        calls.append((page, mode))
        text = "Selecting Assign Mode\n1.Press [MENU]. " * 10
        return text if mode == pdf.MODE_RAW else text.replace("1.Press", "1. Press")

    monkeypatch.setattr(pdf, "_pdfinfo", lambda path: (2, "SH-01A"))
    monkeypatch.setattr(pdf, "_page_text", fake)
    manual = tmp_path / "sh01a.pdf"
    manual.write_bytes(b"%PDF-1.4")

    doc = pdf.PdfExtractor().extract(manual)
    assert sorted(calls) == [(1, "raw"), (1, "reading"), (2, "raw"), (2, "reading")]
    assert [b.extra for b in doc.blocks] == [
        {"page": 1, "extract_mode": "raw"},
        {"page": 2, "extract_mode": "raw"},
    ]
    assert "1. Press [MENU]" in doc.blocks[0].text
    assert "flagged_pages" not in doc.extra


# --- state database --------------------------------------------------------


@pytest.fixture
def read_only_state_dir(tmp_path):
    """data/state as the MCP servers see it: a directory they cannot write."""
    directory = tmp_path / "state"
    directory.mkdir()
    yield directory
    directory.chmod(0o755)


def test_reader_works_on_a_read_only_mount(read_only_state_dir):
    """The writer's file must stay readable after it exits.

    A WAL-mode file cannot be opened read-only once its -wal/-shm files are
    gone and the directory refuses to recreate them, which is exactly the
    MCP servers' `:ro` mount after the one-shot worker exits.
    """
    from app.pipeline import state

    path = read_only_state_dir / "rag.db"
    with state.writer(path) as conn:
        conn.execute(
            "INSERT INTO sources (source_id, domain, source_type) VALUES ('inbox:notes', 'notes', 'inbox')"
        )
    read_only_state_dir.chmod(0o555)

    with state.reader(path) as conn:
        assert [r["source_id"] for r in state.all_sources(conn)] == ["inbox:notes"]


def test_writer_converts_an_existing_wal_database(tmp_path):
    import sqlite3

    from app.pipeline import state

    path = tmp_path / "rag.db"
    legacy = sqlite3.connect(path)
    legacy.execute("PRAGMA journal_mode=WAL")
    legacy.close()

    with state.writer(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_reader_degrades_when_the_file_cannot_be_read(read_only_state_dir):
    from app.pipeline import state

    path = read_only_state_dir / "rag.db"
    path.write_bytes(b"not a database")
    read_only_state_dir.chmod(0o555)

    with state.reader(path) as conn:
        assert state.all_sources(conn) == []


def test_list_sources_includes_inbox_sources_it_cannot_see(tmp_path, monkeypatch):
    """mcp-server has no inbox mount, so inbox sources come from the state db."""
    from app import main
    from app.pipeline import qdrant_store, state

    monkeypatch.setattr(settings, "state_db_path", tmp_path / "rag.db")
    monkeypatch.setattr(settings, "inbox_path", tmp_path / "not-mounted")
    monkeypatch.setattr(settings, "sources_file", tmp_path / "sources.yaml")
    monkeypatch.setattr(qdrant_store, "count_points", lambda *a, **kw: 7)
    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - {id: dev-notes, type: local, domain: notes, path: /tmp}\n"
        "  - {id: old-notes, type: local, domain: notes, path: /tmp, enabled: false}\n"
    )
    with state.writer() as conn:
        for source_id, domain, kind in [
            ("inbox:manuals", "manuals", "inbox"),
            ("dev-notes", "notes", "local"),
            ("old-notes", "notes", "local"),
        ]:
            state.record_source_start(conn, source_id, domain, kind, "run-1")

    listed = {s["source_id"]: s for s in main.list_sources()}
    assert sorted(listed) == ["dev-notes", "inbox:manuals"]
    assert listed["inbox:manuals"]["domain"] == "manuals"
    assert listed["inbox:manuals"]["tier"] == "open"
    assert listed["inbox:manuals"]["chunks"] == 7


# --- document lifecycle ----------------------------------------------------


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    from app.pipeline import state

    monkeypatch.setattr(settings, "state_db_path", tmp_path / "rag.db")
    state.init()
    return tmp_path / "rag.db"


def _doc(conn, doc_id="d1", uri="topology.md", **kw):
    from app.pipeline import state

    fields = dict(
        doc_id=doc_id, source_id="s", domain="notes", tier=Tier.OPEN.value, uri=uri,
        title=uri, content_hash="h", chunk_count=3, extractor="text",
        status=state.INDEXED, last_seen_run="r1",
    )
    fields.update(kw)
    state.upsert_document(conn, **fields)


def test_lifecycle_defaults_to_active(state_db):
    from app.documents import ACTIVE
    from app.pipeline import state

    with state.writer() as conn:
        _doc(conn)
        assert state.get_document(conn, "d1")["lifecycle"] == ACTIVE


def test_lifecycle_is_separate_from_pipeline_status(state_db):
    """`status` says whether the file could be read; `lifecycle` says whether
    its contents should be believed. One column cannot answer both."""
    from app.documents import STALE
    from app.pipeline import state

    with state.writer() as conn:
        _doc(conn)
        state.set_lifecycle(conn, "d1", STALE, reason="hardware retired")
        row = state.get_document(conn, "d1")
        assert row["status"] == state.INDEXED
        assert row["lifecycle"] == STALE


def test_reindexing_does_not_reset_lifecycle(state_db):
    """Editing a superseded file must not quietly make it current again."""
    from app.documents import SUPERSEDED
    from app.pipeline import state

    with state.writer() as conn:
        _doc(conn)
        state.set_lifecycle(conn, "d1", SUPERSEDED, superseded_by="topology-v2.md")
        _doc(conn, content_hash="changed")  # a re-ingest of the same doc_id
        row = state.get_document(conn, "d1")
        assert row["content_hash"] == "changed"
        assert row["lifecycle"] == SUPERSEDED


def test_tombstones_survive_the_disappearance_sweep(state_db):
    """A retracted document is skipped before extraction, so it never gets the
    run marker. If the sweep collected it, the row that keeps it out would go
    and the next run would index it again."""
    from app.documents import RETRACTED
    from app.pipeline import state

    with state.writer() as conn:
        _doc(conn, doc_id="keep", uri="retracted.md")
        _doc(conn, doc_id="drop", uri="deleted.md")
        state.set_lifecycle(conn, "keep", RETRACTED, reason="wrong")

        missing = {r["doc_id"] for r in state.documents_missing_run(conn, "s", "r2")}
        assert missing == {"drop"}
        assert "keep" in state.tombstones(conn, "s")


def test_retracted_documents_have_no_opt_in():
    from app import documents

    every = documents.visible_lifecycles(include_superseded=True, include_stale=True)
    assert documents.RETRACTED not in every
    assert set(every) == {documents.ACTIVE, documents.STALE, documents.SUPERSEDED}


def test_default_search_shows_only_active():
    from app import documents

    assert documents.visible_lifecycles() == [documents.ACTIVE]


def test_review_date_flips_only_when_past(state_db):
    import datetime as dt

    from app.documents import ACTIVE
    from app.pipeline import state

    today = dt.date.today()
    with state.writer() as conn:
        _doc(conn, doc_id="past", uri="old.md")
        _doc(conn, doc_id="future", uri="new.md")
        state.set_lifecycle(
            conn, "past", ACTIVE, review_after=str(today - dt.timedelta(days=1))
        )
        state.set_lifecycle(
            conn, "future", ACTIVE, review_after=str(today + dt.timedelta(days=1))
        )
        due = {r["doc_id"] for r in state.due_for_review(conn, today.isoformat())}
    assert due == {"past"}


def test_stale_banner_is_not_stored_in_the_content(state_db):
    """It is applied at read time, so flipping a flag stays a payload update
    and never becomes a re-embed."""
    from app import documents
    from app.pipeline import qdrant_store

    payload = {
        "content": "Torque to 18 ft-lb.", "uri": "pump.md", "lifecycle": documents.STALE,
        "lifecycle_reason": "superseded hardware", "lifecycle_set_at": "2026-01-01",
        "locator": {"lines": [3, 3]},
    }
    hit = qdrant_store._hit(dict(payload), 0.9)
    assert hit["content"].startswith("[STALE since 2026-01-01: superseded hardware]")
    assert "Torque to 18 ft-lb." in hit["content"]
    assert payload["content"] == "Torque to 18 ft-lb."
    assert hit["citation"] == "pump.md, line 3"


def test_periodic_documents_are_not_flagged_as_versions():
    """A March statement does not retire February's. Auto-detecting would
    silently retire live financial records."""
    from app.pipeline.sync import similar_documents

    rows = [
        {"uri": "statement-2026-02.pdf", "lifecycle": "active"},
        {"uri": "statement-2026-03.pdf", "lifecycle": "active"},
    ]
    assert similar_documents(rows) == []


def test_versioned_filenames_are_offered_as_a_candidate_pair():
    from app.pipeline.sync import similar_documents

    rows = [
        {"uri": "network-topology-v1.md", "lifecycle": "active"},
        {"uri": "network-topology-v2.md", "lifecycle": "active"},
    ]
    assert similar_documents(rows) == [("network-topology-v1.md", "network-topology-v2.md")]


def test_same_named_files_in_different_directories_are_not_versions():
    """Every source tree has many CLAUDE.md and README.md files. Matching on
    the bare filename warns about all of them on every run, and a warning
    nobody reads is worse than none."""
    from app.pipeline.sync import similar_documents

    rows = [
        {"uri": "RAG/CLAUDE.md", "lifecycle": "active"},
        {"uri": "LLM/CLAUDE.md", "lifecycle": "active"},
        {"uri": "RAG/README.md", "lifecycle": "active"},
        {"uri": "SupercellWx/scwx-qt/res/README.md", "lifecycle": "active"},
    ]
    assert similar_documents(rows) == []


def test_versions_in_the_same_directory_are_still_found():
    from app.pipeline.sync import similar_documents

    rows = [
        {"uri": "infra/topology-v1.md", "lifecycle": "active"},
        {"uri": "infra/topology-v2.md", "lifecycle": "active"},
    ]
    assert similar_documents(rows) == [("infra/topology-v1.md", "infra/topology-v2.md")]


def test_superseded_documents_are_not_re_suggested():
    from app.pipeline.sync import similar_documents

    rows = [
        {"uri": "infra/topology-v1.md", "lifecycle": "superseded"},
        {"uri": "infra/topology-v2.md", "lifecycle": "active"},
    ]
    assert similar_documents(rows) == []


def test_scheduling_a_review_does_not_erase_the_existing_reason(state_db):
    """set_lifecycle overwrites lifecycle_reason unconditionally, so reusing it
    to set only a date wiped the note saying why a document was flagged."""
    from app.documents import STALE
    from app.pipeline import state

    with state.writer() as conn:
        _doc(conn)
        state.set_lifecycle(conn, "d1", STALE, reason="hardware decommissioned")
        state.schedule_review(conn, "d1", "2027-01-01")
        row = state.get_document(conn, "d1")
    assert row["lifecycle_reason"] == "hardware decommissioned"
    assert row["review_after"] == "2027-01-01"
    assert row["lifecycle"] == STALE
