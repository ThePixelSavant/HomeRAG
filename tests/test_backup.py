"""Backups (ADR-032): bundle, keyless vault copy, verify, restore, history."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import backup
from app.config import settings
from app.pipeline import qdrant_store, state, sync

PASS = "test passphrase"


class FakeQdrant:
    """Just enough of QdrantClient: snapshots land where the real one puts them."""

    def __init__(self, root: Path, *, down: bool = False):
        self.root = root
        self.down = down
        self.collections = {settings.collection_name: b"points-v1", qdrant_store.meta_collection(): b"meta"}
        self.recovered: list[tuple[str, bytes]] = []
        self.deleted: list[str] = []
        self.cleared: list[str] = []
        self.n = 0

    def _check(self):
        if self.down:
            raise ConnectionError("qdrant unreachable")

    def info(self):
        self._check()
        return SimpleNamespace(version="1.20.0")

    def get_collections(self):
        self._check()
        return list(self.collections)

    def collection_exists(self, name):
        self._check()
        return name in self.collections

    def create_snapshot(self, name, wait=True):
        self._check()
        self.n += 1
        snap = f"{name}-{self.n}.snapshot"
        path = self.root / name / snap
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.collections[name])
        return SimpleNamespace(name=snap, checksum="c" * 64, size=path.stat().st_size)

    def delete_snapshot(self, name, snap, wait=True):
        (self.root / name / snap).unlink()
        self.deleted.append(snap)

    def delete(self, collection_name, points_selector, wait=True):
        # Only clear_collection deletes through here: an empty filter is every point.
        assert points_selector.filter.must is None
        self.collections[collection_name] = b""
        self.cleared.append(collection_name)

    def download(self, collection, snapshot, dest):
        # What qdrant_store.download_snapshot does over HTTP.
        shutil.copyfile(self.root / collection / snapshot.name, dest)

    def recover_snapshot(self, name, location, priority=None, wait=True):
        rel = location.removeprefix(f"file://{backup.QDRANT_SNAPSHOT_ROOT}/")
        data = (self.root / rel).read_bytes()
        self.collections[name] = data
        self.recovered.append((name, data))


@pytest.fixture
def data(tmp_path, monkeypatch, unlocked):
    """A whole data/ tree: rag.db with a row, an unlocked vault holding a blob."""
    from app.vault import blobs
    from app.vault.keyagent import AGENT

    root = tmp_path / "data"
    for name, rel in {
        "data_root": "", "backups_path": "backups", "qdrant_snapshots_path": "qdrant-snapshots",
        "state_db_path": "state/rag.db", "inbox_path": "inbox", "quarantine_path": "quarantine",
        "archive_path": "archive",
    }.items():
        monkeypatch.setattr(settings, name, root / rel if rel else root)
    root.mkdir()
    with state.writer() as conn:
        state.record_eval(conn, questions_file="q.yaml", questions_hash="h", depth=10,
                          summary={"questions": 1, "hit@1": 1.0, "hit@3": 1.0, "mrr@10": 1.0,
                                   "mean_top3_chars": 10}, per_question=[])

    receipt = tmp_path / "receipt.jpg"
    receipt.write_bytes(b"\xff\xd8 a receipt photo")
    conn = AGENT.connect()
    conn.execute(
        "INSERT INTO documents (doc_id, source_id, domain, uri, content_hash, extractor, status) "
        "VALUES ('d1','inbox:receipts','receipts','receipt.jpg','h','receipt','indexed')"
    )
    blobs.store(conn, AGENT.key(), "d1", receipt)
    conn.commit()
    conn.close()

    fake = FakeQdrant(root / "qdrant-snapshots")
    monkeypatch.setattr(qdrant_store, "get_client", lambda: fake)
    monkeypatch.setattr(qdrant_store, "download_snapshot", fake.download)
    monkeypatch.setattr(qdrant_store, "count_points", lambda *a, **k: 7)
    return SimpleNamespace(root=root, vault=unlocked, qdrant=fake)


# --------------------------------------------------------------------------
# bundle
# --------------------------------------------------------------------------


def test_bundle_holds_every_store_and_verifies(data):
    result = backup.create_bundle(wait=0)
    bundle = result["path"]
    assert result["status"] == "ok"
    assert bundle.name.startswith("rag-") and not bundle.name.endswith(".partial")

    manifest = json.loads((bundle / "manifest.json").read_text())
    assert set(manifest["files"]) == {
        "state/rag.db", "vault/vault.db", "vault/vault.salt", "vault/vault.verifier",
        "vault/blobs/d1.enc", f"qdrant/{settings.collection_name}.snapshot",
        f"qdrant/{qdrant_store.meta_collection()}.snapshot",
    }
    assert manifest["vault"] == {"present": True, "blobs": 1, "copy_attempts": 1}
    # Qdrant's own copies are cleaned up after being taken in.
    assert len(data.qdrant.deleted) == 2

    copied = sqlite3.connect(bundle / "state/rag.db")
    assert copied.execute("SELECT questions_file FROM eval_runs").fetchall() == [("q.yaml",)]
    copied.close()
    assert (bundle / "vault/vault.salt").read_bytes() == (data.vault / "vault.salt").read_bytes()

    ok, rows = backup.verify_bundle(bundle)
    assert ok, rows


def test_bundle_is_private(data):
    bundle = backup.create_bundle(wait=0)["path"]
    for path in [bundle, *bundle.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path


def test_bundle_never_contains_env_or_inbox(data):
    (data.root / "inbox" / "manuals").mkdir(parents=True)
    (data.root / "inbox" / "manuals" / "pump.pdf").write_bytes(b"%PDF")
    bundle = backup.create_bundle(wait=0)["path"]
    names = {p.name for p in bundle.rglob("*")}
    assert "pump.pdf" not in names and ".env" not in names


def test_prune_keeps_the_newest(data):
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    made = [backup.create_bundle(wait=0, keep=2, now=t0 + timedelta(days=i))["bundle"] for i in range(3)]
    assert [b.name for b in backup.list_bundles()] == made[1:]


def test_crashed_partial_is_cleared_and_never_listed(data):
    stale = settings.backups_path / "rag-20260101T000000Z.partial"
    stale.mkdir(parents=True)
    (stale / "manifest.json").write_text("{}")
    assert backup.list_bundles() == []
    backup.create_bundle(wait=0)
    assert not stale.exists()


def test_missing_salt_refuses_rather_than_mislead(data):
    (data.vault / "vault.salt").unlink()
    with pytest.raises(backup.BackupError, match="salt"):
        backup.create_bundle(wait=0)
    assert backup.list_bundles() == []


def test_qdrant_down_still_saves_the_rest(data):
    data.qdrant.down = True
    result = backup.create_bundle(wait=0)
    assert result["status"] == "partial"
    assert "qdrant" in result["detail"]
    ok, rows = backup.verify_bundle(result["path"])
    assert ok
    assert any(r[1] == "qdrant" and r[0] == backup.WARN for r in rows)


def test_backup_waits_for_a_running_sync(data):
    with sync.sync_lock():
        with pytest.raises(RuntimeError, match="already running"):
            backup.create_bundle(wait=0)


# --------------------------------------------------------------------------
# the keyless vault copy
# --------------------------------------------------------------------------


def test_copy_stable_retries_when_a_write_lands_mid_copy(tmp_path):
    src, dst = tmp_path / "vault.db", tmp_path / "out" / "vault.db"
    src.write_bytes(b"before")
    calls = []

    def copy_then_write(a, b):
        Path(b).write_bytes(Path(a).read_bytes())
        if not calls:
            src.write_bytes(b"after!")          # an audit row landing during the copy
            st = src.stat()
            os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        calls.append(1)

    attempts = backup.copy_stable(src, dst, copy=copy_then_write, sleep=lambda s: None)
    assert attempts == 2
    assert dst.read_bytes() == b"after!"


def test_copy_stable_gives_up_on_a_persistent_journal(tmp_path):
    src = tmp_path / "vault.db"
    src.write_bytes(b"x")
    (tmp_path / "vault.db-journal").write_bytes(b"j")
    with pytest.raises(backup.BackupError, match="kept changing"):
        backup.copy_stable(src, tmp_path / "o" / "vault.db", sleep=lambda s: None)


def test_copy_stable_refuses_wal(tmp_path):
    src = tmp_path / "vault.db"
    src.write_bytes(b"x")
    (tmp_path / "vault.db-wal").write_bytes(b"w")
    with pytest.raises(backup.BackupError, match="WAL"):
        backup.copy_stable(src, tmp_path / "o" / "vault.db")


def test_vault_copy_is_byte_identical(data):
    bundle = backup.create_bundle(wait=0)["path"]
    assert (bundle / "vault/vault.db").read_bytes() == settings.vault_db_path.read_bytes()


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rel", [
    "state/rag.db", "vault/vault.db", "vault/blobs/d1.enc", "QDRANT",
])
def test_verify_catches_a_flipped_byte(data, rel):
    bundle = backup.create_bundle(wait=0)["path"]
    if rel == "QDRANT":
        rel = f"qdrant/{settings.collection_name}.snapshot"
    path = bundle / rel
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    path.write_bytes(bytes(raw))
    ok, rows = backup.verify_bundle(bundle)
    assert not ok
    assert (backup.BAD, rel, "does not match the manifest") in rows


def test_verify_flags_planted_files(data):
    bundle = backup.create_bundle(wait=0)["path"]
    (bundle / "vault" / "extra.sh").write_text("echo hi")
    ok, rows = backup.verify_bundle(bundle)
    assert ok  # inert: restore copies manifest entries only
    assert any(r[1] == "unexpected files" and "vault/extra.sh" in r[2] for r in rows)


def test_verify_opens_the_vault_with_the_right_passphrase(data):
    bundle = backup.create_bundle(wait=0)["path"]
    ok, rows = backup.verify_bundle(bundle, passphrase=PASS)
    assert ok, rows
    assert (backup.OK, "vault blobs", "all 1 decrypt and match") in rows
    # Opening it from a temp copy left the bundle untouched.
    assert backup.verify_bundle(bundle)[0]


def test_verify_rejects_the_wrong_passphrase(data):
    bundle = backup.create_bundle(wait=0)["path"]
    ok, rows = backup.verify_bundle(bundle, passphrase="not it")
    assert not ok
    assert any("does not open" in r[2] for r in rows)


def test_verify_catches_a_tampered_blob_under_the_key(data):
    """A blob replaced wholesale, manifest re-hashed to match: GCM still catches it."""
    bundle = backup.create_bundle(wait=0)["path"]
    blob = bundle / "vault/blobs/d1.enc"
    blob.write_bytes(b"\x00" * 64)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["files"]["vault/blobs/d1.enc"] = {"sha256": backup._sha256(blob), "size": 64}
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    ok, rows = backup.verify_bundle(bundle, passphrase=PASS)
    assert not ok
    assert any(r[1] == "vault blobs" and r[0] == backup.BAD for r in rows)


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------


def test_restore_moves_the_current_data_aside_and_puts_the_bundle_back(data):
    bundle = backup.create_bundle(wait=0)["path"]
    # Life goes on after the backup...
    with state.writer() as conn:
        conn.execute("DELETE FROM eval_runs")
    settings.vault_db_path.write_bytes(b"corrupted")
    data.qdrant.collections[settings.collection_name] = b"points-v2"

    backup.restore_bundle(bundle, now=datetime(2026, 11, 8, tzinfo=timezone.utc))

    with state.reader() as conn:
        assert len(state.eval_history(conn)) == 1
    assert settings.vault_db_path.read_bytes() == (bundle / "vault/vault.db").read_bytes()
    assert data.qdrant.collections[settings.collection_name] == b"points-v1"
    # Nothing was deleted: the replaced files and index are kept.
    aside = data.root / "pre-restore-20261108T000000Z"
    assert (aside / "vault" / "vault.db").read_bytes() == b"corrupted"
    assert (aside / "state" / "rag.db").exists()
    assert (aside / "qdrant" / f"{settings.collection_name}.snapshot").read_bytes() == b"points-v2"
    # The staged snapshot copies are gone from Qdrant's directory.
    assert not list((data.root / "qdrant-snapshots").rglob("restore-*"))


def test_a_tenant_restore_empties_the_index_and_re_embeds_instead(data, monkeypatch):
    # A tenant token cannot recover a snapshot (ADR-035). The restore must
    # still put rag.db back, keep the current index aside, and leave the next
    # ingest to rebuild the index from what the restored rag.db describes.
    from app.pipeline import state as st

    with st.writer() as conn:
        st.upsert_document(
            conn, doc_id="d-open", source_id="inbox:notes", domain="notes", tier="open",
            uri="a.md", title="a", content_hash="h1", chunk_count=1, extractor="text",
            status=st.INDEXED, last_seen_run="r1",
        )
        st.upsert_document(
            conn, doc_id="d-gone", source_id="inbox:notes", domain="notes", tier="open",
            uri="b.md", title="b", content_hash="h2", chunk_count=1, extractor="text",
            status=st.INDEXED, last_seen_run="r1",
        )
        st.set_lifecycle(conn, "d-gone", "retracted", reason="wrong")
    bundle = backup.create_bundle(wait=0)["path"]
    data.qdrant.collections[settings.collection_name] = b"points-v2"
    monkeypatch.setattr(qdrant_store, "is_tenant", lambda *a: True)

    log = backup.restore_bundle(bundle, now=datetime(2026, 11, 8, tzinfo=timezone.utc))

    assert data.qdrant.recovered == []
    assert data.qdrant.cleared == [settings.collection_name]
    aside = data.root / "pre-restore-20261108T000000Z"
    assert (aside / "qdrant" / f"{settings.collection_name}.snapshot").read_bytes() == b"points-v2"
    with st.reader() as conn:
        assert st.get_document(conn, "d-open")["content_hash"] == ""
        # A tombstone keeps its hash and its row: re-embedding must not revive it.
        assert st.get_document(conn, "d-gone")["content_hash"] == "h2"
    assert any("index cleared" in line for line in log)


def test_restore_never_merges_into_a_live_inbox(data, tmp_path):
    bundle = backup.create_bundle(wait=0)["path"]
    restored = tmp_path / "restored-inbox"
    (restored / "manuals").mkdir(parents=True)
    (restored / "manuals" / "old.pdf").write_bytes(b"%PDF old")
    (data.root / "inbox" / "notes").mkdir(parents=True)
    (data.root / "inbox" / "notes" / "live.md").write_text("live")

    log = backup.restore_bundle(bundle, trees={"inbox": restored})
    assert not (data.root / "inbox" / "manuals" / "old.pdf").exists()
    assert any("not empty" in line for line in log)


def test_restore_fills_an_empty_inbox(data, tmp_path):
    bundle = backup.create_bundle(wait=0)["path"]
    restored = tmp_path / "restored-inbox"
    (restored / "manuals").mkdir(parents=True)
    (restored / "manuals" / "pump.pdf").write_bytes(b"%PDF")
    backup.restore_bundle(bundle, trees={"inbox": restored})
    assert (data.root / "inbox" / "manuals" / "pump.pdf").read_bytes() == b"%PDF"


def test_restore_refuses_an_unverified_bundle(data):
    bundle = backup.create_bundle(wait=0)["path"]
    (bundle / "state/rag.db").write_bytes(b"tampered")
    with pytest.raises(backup.BackupError, match="unverified"):
        backup.restore_bundle(bundle)
    assert not list(data.root.glob("pre-restore-*"))


def test_restore_refuses_while_a_sync_runs(data):
    bundle = backup.create_bundle(wait=0)["path"]
    with sync.sync_lock():
        with pytest.raises(RuntimeError, match="already running"):
            backup.restore_bundle(bundle)


def test_compatibility_names_a_model_change(data, monkeypatch):
    bundle = backup.create_bundle(wait=0)["path"]
    assert backup.compatibility(bundle) == []
    monkeypatch.setattr(settings, "embed_model", "other/model")
    assert any("embed_model" in n for n in backup.compatibility(bundle))


# --------------------------------------------------------------------------
# lock, history, doctor, dashboard
# --------------------------------------------------------------------------


def test_sync_lock_waits_then_takes_it(tmp_path):
    held = threading.Event()
    release = threading.Event()

    def holder():
        with sync.sync_lock(tmp_path):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    threading.Timer(0.3, release.set).start()
    start = time.monotonic()
    with sync.sync_lock(tmp_path, wait=5):
        waited = time.monotonic() - start
    t.join()
    assert 0.2 < waited < 4


def test_sync_lock_times_out(tmp_path):
    with sync.sync_lock(tmp_path):
        start = time.monotonic()
        with pytest.raises(RuntimeError):
            with sync.sync_lock(tmp_path, wait=0.3):
                pass
        assert time.monotonic() - start >= 0.25


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db_path", tmp_path / "rag.db")
    state.init()

    def add(kind, target, status, *, days_ago=0.0, **kw):
        with state.writer() as conn:
            state.record_backup(conn, kind=kind, target=target, status=status, **kw)
            when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
            conn.execute("UPDATE backup_runs SET run_at=? WHERE id=(SELECT MAX(id) FROM backup_runs)", (when,))
    return add


def test_overview_levels(history):
    history("local", "local", "ok", days_ago=0.5)
    history("offsite", "nas", "ok", days_ago=5)
    history("offsite", "usb", "ok", days_ago=10, max_age_days=30)
    history("offsite", "usb", "skipped", max_age_days=30)
    history("offsite", "old", "ok", days_ago=1)
    history("offsite", "old", "rollback")
    with state.reader() as conn:
        by = {b["target"]: b for b in state.backup_overview(conn)}
    assert by["local"]["level"] == "ok"
    assert by["nas"]["level"] == "warn" and "5.0 days" in by["nas"]["why"]
    assert by["usb"]["level"] == "ok"           # skipped, but inside its 30-day limit
    assert by["old"]["level"] == "bad"


def test_overview_tolerates_a_database_without_the_table(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    assert state.backup_overview(conn) == []


def test_record_rejects_unknown_status(history):
    with pytest.raises(ValueError):
        history("offsite", "nas", "fine")


def test_doctor_rows(history):
    from app import ingest

    assert ingest._backup_rows()[0][0] == ingest.WARN    # never run
    history("local", "local", "ok")
    history("offsite", "nas", "rollback")
    rows = {label: level for level, label, _ in ingest._backup_rows()}
    assert rows == {"backup local": ingest.OK, "backup nas": ingest.BAD}


def test_dashboard_shows_backups_without_paths_or_errors(history, monkeypatch):
    from app import report, report_html

    monkeypatch.setattr(qdrant_store, "count_points", lambda *a, **k: 7)
    history("local", "local", "ok")
    history("offsite", "nas", "failed", detail="Fatal: unable to open /mnt/nas/dpbu/rag-restic")
    with state.reader() as conn:
        data = report.build("overview", conn, {})
    page = report_html.render("overview", data)
    assert "/mnt/nas" not in json.dumps(data, default=str) and "/mnt/nas" not in page
    assert "nas" in page and "today" in page


def test_cmd_backup_records_the_run(data, capsys):
    from app import ingest

    assert ingest.main(["backup"]) == 0
    with state.reader() as conn:
        rows = state.backup_overview(conn)
    assert rows[0]["target"] == "local" and rows[0]["status"] == "ok"
    assert rows[0]["snapshot_id"].startswith("rag-")


def test_cmd_backup_record(history):
    from app import ingest

    assert ingest.main(["backup-record", "--target", "nas", "--status", "ok",
                        "--snapshot-id", "abc", "--snapshot-count", "3"]) == 0
    with state.reader() as conn:
        (row,) = state.backup_overview(conn)
    assert (row["kind"], row["snapshot_count"]) == ("offsite", 3)
