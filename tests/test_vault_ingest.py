"""Vault-tier ingestion, and the process boundary it runs across.

This path had no coverage at all, which is why it shipped unable to run: the
ingestion worker checked its own in-process `AGENT`, which `make unlock` never
touches because that unlocks the vault *server* in a different container. The
first test here would have caught it.

The boundary being asserted is not incidental. Extraction parses
attacker-influenced bytes; the parent holds a key that decrypts every document
ever stored. `test_parse_worker_imports_nothing_key_bearing` is the one that
keeps those apart as the code changes.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.pipeline import state, sync
from app.sources import LOCAL, Source
from app.vault import store as vault_store
from app.vault.keyagent import AGENT


@pytest.fixture
def vault_source(unlocked, tmp_path, monkeypatch):
    """A vault-tier source with one real file in it."""
    from app.config import settings

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text(
        "# Quarterly spend\n\n"
        "Paid the plumber 240 on the 3rd. The pump motor was replaced under\n"
        "warranty, so the invoice shows zero for parts and 180 for labour.\n"
    )
    monkeypatch.setattr(settings, "state_db_path", tmp_path / "state.db")
    monkeypatch.setattr(settings, "data_root", tmp_path)
    state.init()
    return Source(id="s-vault", type=LOCAL, domain="receipts", path=docs)


def _sync(source, run_id="run-1"):
    return sync.sync_source(source, run_id)


# --- the bug this file exists for -----------------------------------------


def test_vault_source_ingests_end_to_end(vault_source):
    """The whole point: a vault document reaches the encrypted store.

    Before the parse/write split this returned queued_sealed even with the
    vault open, because the worker asked its own empty key agent.
    """
    result = _sync(vault_source)

    assert result.queued_sealed == 0, f"still reporting sealed: {result.notes}"
    assert result.docs_indexed == 1, f"nothing indexed: {result.notes}"
    assert result.chunks_upserted >= 1

    conn = AGENT.connect()
    try:
        rows = conn.execute("SELECT doc_id, title, chunk_count FROM documents").fetchall()
        assert len(rows) == 1
        assert rows[0]["chunk_count"] == result.chunks_upserted
        chunks = conn.execute("SELECT text, vector FROM chunks").fetchall()
        assert chunks, "document row written with no chunks"
        # The vector survived the base64 round trip through the child.
        assert len(vault_store.unpack_vector(chunks[0]["vector"])) > 0
        assert "plumber" in " ".join(c["text"] for c in chunks)
    finally:
        conn.close()


def test_sealed_vault_ingests_nothing(vault_source):
    """Sealed still means sealed -- the split must not have opened a side door."""
    AGENT.lock()
    result = _sync(vault_source)

    assert result.queued_sealed == 1
    assert result.docs_indexed == 0


def test_unchanged_document_is_not_re_embedded(vault_source):
    """Second run should touch the row, not re-chunk and re-embed it."""
    first = _sync(vault_source, run_id="run-1")
    assert first.docs_indexed == 1

    second = _sync(vault_source, run_id="run-2")
    assert second.docs_indexed == 0
    assert second.docs_skipped == 1
    assert second.chunks_upserted == 0


def test_changed_document_is_reindexed(vault_source):
    _sync(vault_source, run_id="run-1")
    (vault_source.path / "note.md").write_text("# Quarterly spend\n\nReplaced entirely.\n")

    result = _sync(vault_source, run_id="run-2")
    assert result.docs_indexed == 1

    conn = AGENT.connect()
    try:
        texts = " ".join(r["text"] for r in conn.execute("SELECT text FROM chunks"))
        assert "Replaced entirely" in texts
        assert "plumber" not in texts, "old chunks not replaced"
    finally:
        conn.close()


# --- the process boundary --------------------------------------------------


def test_parse_worker_imports_nothing_key_bearing():
    """The extraction half must not be able to reach a key, even by accident.

    Asserted in a fresh interpreter rather than this one, because the test
    session has already imported half the project.
    """
    probe = (
        "import sys; from app.pipeline import parse_worker; "
        "print(','.join(m for m in ('app.vault.keyagent','app.vault.crypto',"
        "'app.pipeline.sync') if m in sys.modules))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    leaked = proc.stdout.strip()
    assert not leaked, f"parse_worker pulled key-bearing modules into its process: {leaked}"


def test_parse_worker_reports_bad_input_without_crashing():
    proc = subprocess.run(
        [sys.executable, "-m", "app.pipeline.parse_worker"],
        input="not json",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "unreadable job" in proc.stdout


def test_parse_worker_failure_surfaces_as_an_error(vault_source, monkeypatch):
    """A dead child must fail the source loudly, not silently index nothing."""

    def boom(*a, **kw):
        raise sync.ParseWorkerFailed("child exited 1: segfault")

    monkeypatch.setattr(sync, "_run_parse_worker", boom)
    with pytest.raises(sync.ParseWorkerFailed):
        _sync(vault_source)


# --- how the worker gets a key --------------------------------------------


def _vault_sources():
    return [Source(id="s-vault", type=LOCAL, domain="receipts", path=None)]


def test_no_terminal_queues_instead_of_prompting(monkeypatch, vault_dir):
    """A scheduled run has no TTY. It must queue quietly, not hang or crash.

    The passphrase is never read from a flag, a file or the environment: that
    is the mechanism that stops a model unlocking the vault, since a model has
    no terminal.
    """
    from app import ingest

    monkeypatch.setattr(ingest.sys.stdin, "isatty", lambda: False, raising=False)

    def fail(*a, **kw):
        raise AssertionError("prompted for a passphrase without a terminal")

    monkeypatch.setattr(ingest.getpass, "getpass", fail)
    ingest._unlock_for_vault_sources(_vault_sources())
    assert not AGENT.is_unlocked()


def test_already_unlocked_does_not_prompt(monkeypatch, unlocked):
    from app import ingest

    def fail(*a, **kw):
        raise AssertionError("prompted although the key was already held")

    monkeypatch.setattr(ingest.getpass, "getpass", fail)
    ingest._unlock_for_vault_sources(_vault_sources())
    assert AGENT.is_unlocked()


def test_open_tier_only_never_prompts(monkeypatch, vault_dir):
    from app import ingest

    def fail(*a, **kw):
        raise AssertionError("prompted for an open-tier-only run")

    monkeypatch.setattr(ingest.getpass, "getpass", fail)
    monkeypatch.setattr(ingest.sys.stdin, "isatty", lambda: True, raising=False)
    ingest._unlock_for_vault_sources([Source(id="s-open", type=LOCAL, domain="manuals", path=None)])


def test_wrong_passphrase_aborts_the_run(monkeypatch, unlocked):
    from app import ingest

    AGENT.lock()
    monkeypatch.setattr(ingest.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(ingest.getpass, "getpass", lambda *a, **kw: "not the passphrase")
    with pytest.raises(SystemExit):
        ingest._unlock_for_vault_sources(_vault_sources())


def test_blank_passphrase_skips_without_unlocking(monkeypatch, unlocked):
    from app import ingest

    AGENT.lock()
    monkeypatch.setattr(ingest.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(ingest.getpass, "getpass", lambda *a, **kw: "")
    ingest._unlock_for_vault_sources(_vault_sources())
    assert not AGENT.is_unlocked()
