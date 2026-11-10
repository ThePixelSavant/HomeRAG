"""Backups (ADR-032): a consistent bundle of every store, verified and restorable.

Runs in the ingestion worker, which mounts ./data read-write and holds the
Qdrant main key, or on a tenant's machine a token for its own collections
(ADR-035): snapshots are downloaded through Qdrant's API either way. Taking the bundle never needs the vault key -- vault.db and the
blobs are copied as ciphertext -- and only `verify --vault` derives one, from a
passphrase typed at a terminal, in this CLI process (never in parse_worker).

Off-machine copies are not made here. `scripts/rag-backup` runs restic on the
host against the newest bundle, because the NAS and USB mounts and the restic
password live on the host, and the password must never sit in `.env`, which
this container receives whole.

A bundle is an ALLOWLIST, never "everything under data/":

    rag-<UTC stamp>/
        manifest.json         sha256 + size of every file below, and the settings
        state/rag.db          sqlite online-backup copy, integrity-checked
        vault/vault.db        ciphertext, copied only when no write is in flight
        vault/vault.salt      REQUIRED: without it the passphrase cannot rebuild the key
        vault/vault.verifier
        vault/blobs/*.enc     AES-GCM originals -- receipts exist nowhere else
        qdrant/<name>.snapshot

It holds no key and no .env.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import settings
from app.domains import SCHEMA_VERSION, Tier, domains_in
from app.pipeline import qdrant_store, state, sync

logger = logging.getLogger("backup")

FORMAT = 1
PREFIX = "rag-"
PARTIAL = ".partial"
MANIFEST = "manifest.json"
# How long a scheduled backup waits for a running ingest before giving up.
SYNC_WAIT_SECONDS = 1800
# data/qdrant-snapshots as the Qdrant container sees it.
QDRANT_SNAPSHOT_ROOT = "/qdrant/snapshots"

OK, WARN, BAD = "ok", "warn", "FAIL"


class BackupError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _stamp(now: datetime | None = None) -> str:
    return f"{(now or datetime.now(timezone.utc)):%Y%m%dT%H%M%SZ}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _private(path: Path) -> None:
    """0700 directories, 0600 files, all the way down."""
    for root, dirs, files in os.walk(path):
        os.chmod(root, 0o700)
        for name in files:
            os.chmod(Path(root) / name, 0o600)


def list_bundles(root: Path | None = None) -> list[Path]:
    """Complete bundles, oldest first. A `.partial` directory is never one."""
    root = root or settings.backups_path
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and p.name.startswith(PREFIX) and not p.name.endswith(PARTIAL)
        and (p / MANIFEST).is_file()
    )


def resolve_bundle(name: str | None, root: Path | None = None) -> Path:
    """A bundle by name, by path, or the newest when `name` is empty."""
    root = root or settings.backups_path
    if not name:
        bundles = list_bundles(root)
        if not bundles:
            raise BackupError(f"No backups in {root} yet. Run `rag backup`.")
        return bundles[-1]
    path = Path(name)
    if not path.is_absolute():
        path = root / name
    if not (path / MANIFEST).is_file():
        raise BackupError(f"{path} is not a backup (no {MANIFEST}).")
    return path


# --------------------------------------------------------------------------
# copying each store consistently
# --------------------------------------------------------------------------


def copy_state_db(src: Path, dst: Path) -> None:
    """SQLite's online backup API: a consistent copy even mid-write."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
    target = sqlite3.connect(str(dst))
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"copy of {src.name} failed its integrity check: {result}")
    finally:
        target.close()
        source.close()


def copy_stable(
    src: Path,
    dst: Path,
    *,
    attempts: int = 5,
    delay: float = 0.5,
    copy: Callable[[Path, Path], object] = shutil.copyfile,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Copy a database this process cannot open, with no write in flight.

    The vault is SQLCipher and the worker holds no key, so the backup API is
    out of reach. mcp-vault writes an audit row on every vault query even when
    ingest is idle. Under its rollback journal every write creates
    `<db>-journal` and moves the mtime, so a copy taken while no journal
    exists, and with size and mtime unchanged across it, saw no write.
    Returns the number of attempts it took.
    """
    journal = src.with_name(src.name + "-journal")
    wal = src.with_name(src.name + "-wal")
    if wal.exists():
        # A WAL database keeps committed pages outside the main file; a file
        # copy would silently lose them.
        raise BackupError(f"{src.name} is in WAL mode; a plain copy would miss committed data.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, attempts + 1):
        if not journal.exists():
            before = src.stat()
            copy(src, dst)
            after = src.stat()
            if not journal.exists() and (before.st_size, before.st_mtime_ns) == (
                after.st_size, after.st_mtime_ns
            ):
                return attempt
        sleep(delay * attempt)
    raise BackupError(
        f"{src.name} kept changing across {attempts} attempts; is something writing to it "
        "continuously? Try again, or stop mcp-vault for the backup."
    )


def _copy_vault(dest: Path) -> dict:
    vault_dir = settings.vault_db_path.parent
    db = settings.vault_db_path
    if not db.exists():
        return {"present": False, "blobs": 0}
    salt = vault_dir / "vault.salt"
    if not salt.exists():
        # Copying vault.db without its salt would produce a backup that looks
        # complete and can never be opened. Refuse rather than mislead.
        raise BackupError(f"{salt} is missing: the vault cannot be restored without it.")
    out = dest / "vault"
    attempts = copy_stable(db, out / "vault.db")
    shutil.copyfile(salt, out / "vault.salt")
    verifier = vault_dir / "vault.verifier"
    if verifier.exists():
        shutil.copyfile(verifier, out / "vault.verifier")
    blobs = 0
    if settings.vault_blobs_path.is_dir():
        for blob in sorted(settings.vault_blobs_path.iterdir()):
            if not blob.is_file():
                continue
            (out / "blobs").mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(blob, out / "blobs" / blob.name)
                blobs += 1
            except FileNotFoundError:
                # Retracted between listing and copying: it is meant to be gone.
                continue
    return {"present": True, "blobs": blobs, "copy_attempts": attempts}


def _collections() -> list[str]:
    return [settings.collection_name, qdrant_store.meta_collection()]


def _snapshot_qdrant(dest: Path) -> dict:
    """Qdrant's own snapshot API, so the index is consistent and restorable."""
    client = qdrant_store.get_client()
    info: dict = {"version": getattr(client.info(), "version", None), "collections": {}}
    out = dest / "qdrant"
    for name in _collections():
        if not client.collection_exists(name):
            continue
        snap = client.create_snapshot(name, wait=True)
        if snap is None:
            raise BackupError(f"Qdrant returned no snapshot for {name}")
        try:
            out.mkdir(parents=True, exist_ok=True)
            qdrant_store.download_snapshot(name, snap, out / f"{name}.snapshot")
        finally:
            try:
                client.delete_snapshot(name, snap.name, wait=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not delete snapshot %s/%s: %s", name, snap.name, exc)
        info["collections"][name] = {
            "file": f"qdrant/{name}.snapshot",
            "checksum": snap.checksum,
        }
    info["points_by_domain"] = {
        d: qdrant_store.count_points(qdrant_store.build_filter(domains=[d]))
        for d in domains_in(Tier.OPEN)
    }
    return info


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def create_bundle(
    *,
    root: Path | None = None,
    keep: int | None = None,
    wait: float = SYNC_WAIT_SECONDS,
    now: datetime | None = None,
) -> dict:
    """Take a bundle. Returns {bundle, path, status, bytes, detail, pruned}.

    `status` is "partial" when Qdrant could not be snapshotted: rag.db and the
    vault still matter more, and the index can be rebuilt from sources.
    """
    root = root or settings.backups_path
    keep = settings.backup_keep_local if keep is None else keep
    if keep < 1:
        raise BackupError("BACKUP_KEEP_LOCAL must be at least 1.")
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)

    name = f"{PREFIX}{_stamp(now)}"
    final = root / name
    work = root / f"{name}{PARTIAL}"
    detail: list[str] = []
    status = "ok"

    with sync.sync_lock(wait=wait):
        # Holding the lock, so any .partial left here is from a crashed run.
        for stale in root.glob(f"{PREFIX}*{PARTIAL}"):
            shutil.rmtree(stale, ignore_errors=True)
        if final.exists():
            raise BackupError(f"{final} already exists")
        work.mkdir(mode=0o700)
        try:
            manifest: dict = {
                "format": FORMAT,
                "bundle": name,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "schema_version": SCHEMA_VERSION,
                "collection": settings.collection_name,
                "embed_model": settings.embed_model,
                "embed_dim": settings.embed_dim,
                "chunk_target_tokens": settings.chunk_target_tokens,
            }
            if settings.state_db_path.exists():
                copy_state_db(settings.state_db_path, work / "state" / "rag.db")
            else:
                detail.append("no rag.db yet")
            manifest["vault"] = _copy_vault(work)
            try:
                manifest["qdrant"] = _snapshot_qdrant(work)
            except Exception as exc:  # noqa: BLE001
                status = "partial"
                manifest["qdrant"] = {"error": f"{type(exc).__name__}: {exc}"}
                detail.append(f"qdrant not snapshotted: {type(exc).__name__}: {exc}")
                shutil.rmtree(work / "qdrant", ignore_errors=True)

            files = {}
            for path in sorted(p for p in work.rglob("*") if p.is_file()):
                rel = path.relative_to(work).as_posix()
                files[rel] = {"sha256": _sha256(path), "size": path.stat().st_size}
            manifest["files"] = files
            (work / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
            _private(work)
            work.rename(final)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise

    size = sum(f["size"] for f in files.values())
    return {
        "bundle": name,
        "path": final,
        "status": status,
        "bytes": size,
        "detail": "; ".join(detail),
        "pruned": prune(root, keep),
    }


def prune(root: Path, keep: int) -> list[str]:
    """Delete all but the newest `keep` bundles. Never the newest."""
    keep = max(1, keep)
    bundles = list_bundles(root)
    gone = []
    for old in bundles[:-keep]:
        shutil.rmtree(old)
        gone.append(old.name)
    return gone


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def verify_bundle(bundle: Path, *, passphrase: str | None = None) -> tuple[bool, list[tuple[str, str, str]]]:
    """Check a bundle against its manifest; with a passphrase, open the vault too.

    Never writes to the bundle: the vault is opened from a temporary copy.
    """
    rows: list[tuple[str, str, str]] = []
    try:
        manifest = json.loads((bundle / MANIFEST).read_text())
    except (OSError, ValueError) as exc:
        return False, [(BAD, "manifest", f"unreadable: {exc}")]
    if manifest.get("format") != FORMAT:
        return False, [(BAD, "manifest", f"unknown format {manifest.get('format')!r}")]

    files: dict = manifest.get("files", {})
    bad = 0
    for rel, meta in files.items():
        path = bundle / rel
        if not path.is_file():
            rows.append((BAD, rel, "missing"))
            bad += 1
        elif path.stat().st_size != meta["size"] or _sha256(path) != meta["sha256"]:
            rows.append((BAD, rel, "does not match the manifest"))
            bad += 1
    if not bad:
        rows.append((OK, "files", f"{len(files)} match the manifest"))
    extra = sorted(
        p.relative_to(bundle).as_posix() for p in bundle.rglob("*")
        if p.is_file() and p.name != MANIFEST and p.relative_to(bundle).as_posix() not in files
    )
    if extra:
        # Restore copies only manifest entries, so these are inert -- but
        # nothing writes them, so something else did.
        rows.append((WARN, "unexpected files", ", ".join(extra[:5])))

    if "state/rag.db" in files and (bundle / "state/rag.db").is_file():
        conn = sqlite3.connect(f"file:{bundle / 'state/rag.db'}?mode=ro&immutable=1", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            result = str(exc)
        finally:
            conn.close()
        rows.append((OK if result == "ok" else BAD, "rag.db", "integrity ok" if result == "ok" else result))

    qdrant = manifest.get("qdrant", {})
    if "error" in qdrant:
        rows.append((WARN, "qdrant", f"not in this bundle: {qdrant['error'][:80]}"))
    else:
        names = ", ".join(qdrant.get("collections", {})) or "none"
        rows.append((OK, "qdrant", f"snapshots: {names}"))

    vault = manifest.get("vault", {})
    if vault.get("present"):
        if "vault/vault.salt" not in files:
            rows.append((BAD, "vault.salt", "missing: the vault in this bundle cannot be opened"))
        elif passphrase is None:
            rows.append((OK, "vault", f"present, {vault.get('blobs', 0)} blob(s); not opened "
                                      "(VAULT=1 checks the passphrase opens it)"))
        else:
            rows.extend(_verify_vault(bundle, passphrase))
    else:
        rows.append((OK, "vault", "none in this bundle"))

    return not any(r[0] == BAD for r in rows), rows


def _verify_vault(bundle: Path, passphrase: str) -> list[tuple[str, str, str]]:
    from app.vault import crypto

    salt = (bundle / "vault/vault.salt").read_bytes()
    key = crypto.derive_key(passphrase, salt)
    verifier = bundle / "vault/vault.verifier"
    if verifier.is_file() and not hmac.compare_digest(
        verifier.read_bytes(), crypto._verifier_for(key)  # noqa: SLF001 -- same check unlock uses
    ):
        return [(BAD, "vault", "that passphrase does not open this bundle's vault")]

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "vault.db"
        shutil.copyfile(bundle / "vault/vault.db", copy)
        try:
            conn = crypto.connect(key, copy)
        except crypto.WrongPassphrase:
            return [(BAD, "vault", "that passphrase does not open this bundle's vault")]
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            rows.append((OK if result == "ok" else BAD, "vault.db",
                         "decrypts, integrity ok" if result == "ok" else result))
            blob_rows = conn.execute("SELECT doc_id, rel_path, nonce, sha256 FROM blobs").fetchall()
        finally:
            conn.close()

    broken = []
    for row in blob_rows:
        path = bundle / "vault/blobs" / row["rel_path"]
        try:
            plain = crypto.decrypt_blob(key, row["nonce"], path.read_bytes())
            if hashlib.sha256(plain).hexdigest() != row["sha256"]:
                broken.append(row["rel_path"])
        except Exception:  # noqa: BLE001 -- missing file or a failed GCM tag alike
            broken.append(row["rel_path"])
    if broken:
        rows.append((BAD, "vault blobs", f"{len(broken)} of {len(blob_rows)} fail: {', '.join(broken[:3])}"))
    else:
        rows.append((OK, "vault blobs", f"all {len(blob_rows)} decrypt and match"))
    return rows


def compatibility(bundle: Path) -> list[str]:
    """Differences between the bundle's settings and this install's."""
    manifest = json.loads((bundle / MANIFEST).read_text())
    current = {
        "schema_version": SCHEMA_VERSION,
        "embed_model": settings.embed_model,
        "embed_dim": settings.embed_dim,
        "collection": settings.collection_name,
    }
    return [
        f"{key} was {manifest.get(key)!r} when backed up, is {now!r} here"
        for key, now in current.items()
        if manifest.get(key) != now
    ]


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------


def _has_files(path: Path) -> bool:
    return path.is_dir() and any(p.is_file() for p in path.rglob("*"))


def _move_aside(src: Path, aside: Path) -> bool:
    if not src.exists():
        return False
    aside.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(aside))
    return True


def _snapshot_current_qdrant(aside: Path) -> list[str]:
    """Keep the index being replaced, the same way the files are kept."""
    client = qdrant_store.get_client()
    kept = []
    for name in _collections():
        if not client.collection_exists(name):
            continue
        snap = client.create_snapshot(name, wait=True)
        if snap is None:
            continue
        aside.mkdir(parents=True, exist_ok=True)
        try:
            qdrant_store.download_snapshot(name, snap, aside / f"{name}.snapshot")
        finally:
            client.delete_snapshot(name, snap.name, wait=True)
        kept.append(name)
    return kept


def restore_bundle(
    bundle: Path,
    *,
    trees: dict[str, Path] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Put a verified bundle back. The caller has stopped every server but Qdrant.

    Nothing current is deleted: rag.db, the vault and the live index all go to
    data/pre-restore-<stamp>/ first. `trees` maps inbox/quarantine/archive to
    restored copies of them; each is filled in only where the live directory
    holds no files, because merging into a live inbox would resurrect
    documents deleted since the backup.
    """
    ok, rows = verify_bundle(bundle)
    if not ok:
        bad = "; ".join(f"{label}: {detail}" for status, label, detail in rows if status == BAD)
        raise BackupError(f"refusing to restore an unverified bundle: {bad}")
    manifest = json.loads((bundle / MANIFEST).read_text())
    files: dict = manifest["files"]
    client = qdrant_store.get_client()
    try:
        client.get_collections()
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"Qdrant is not reachable ({exc}); start it with `docker compose up -d qdrant`.") from exc

    stamp = _stamp(now)
    aside = settings.data_root / f"pre-restore-{stamp}"
    log: list[str] = []

    with sync.sync_lock(wait=0):
        aside.mkdir(parents=True, mode=0o700)

        # rag.db
        if "state/rag.db" in files:
            db = settings.state_db_path
            if _move_aside(db, aside / "state" / db.name):
                _move_aside(db.with_name(db.name + "-journal"), aside / "state" / (db.name + "-journal"))
            db.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundle / "state/rag.db", db)
            log.append(f"rag.db restored (previous one in {aside.name}/state)")

        # vault: move each item, keep the directory -- it is a bind-mount source.
        if manifest.get("vault", {}).get("present"):
            vault_dir = settings.vault_db_path.parent
            for item in ("vault.db", "vault.db-journal", "vault.salt", "vault.verifier"):
                _move_aside(vault_dir / item, aside / "vault" / item)
            _move_aside(settings.vault_blobs_path, aside / "vault" / "blobs")
            vault_dir.mkdir(parents=True, exist_ok=True)
            for rel in files:
                if rel.startswith("vault/"):
                    target = vault_dir / rel.removeprefix("vault/")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(bundle / rel, target)
                    os.chmod(target, 0o600)
            log.append(f"vault restored (previous one in {aside.name}/vault)")

        # qdrant
        collections = manifest.get("qdrant", {}).get("collections", {})
        if qdrant_store.is_tenant():
            # Recovering a snapshot needs the server's main key, which a tenant
            # does not hold (ADR-035). The index is derived, so empty it and
            # let the next run re-embed what the restored rag.db describes;
            # keeping it would leave chunks of anything indexed since.
            kept = _snapshot_current_qdrant(aside / "qdrant")
            if kept:
                log.append(f"current index snapshotted to {aside.name}/qdrant ({', '.join(kept)})")
            qdrant_store.clear_collection()
            with state.writer() as conn:
                marked = state.mark_for_reembed(conn)
            log.append(
                f"index cleared, not recovered: this install's Qdrant token is scoped to its "
                f"own collections. `rag ingest` re-embeds the {marked} open-tier document(s) "
                "the restored rag.db describes"
            )
        elif collections:
            kept = _snapshot_current_qdrant(aside / "qdrant")
            if kept:
                log.append(f"current index snapshotted to {aside.name}/qdrant ({', '.join(kept)})")
            from qdrant_client.http import models as qm

            for name, meta in collections.items():
                staged_name = f"restore-{stamp}.snapshot"
                staged = settings.qdrant_snapshots_path / name / staged_name
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bundle / meta["file"], staged)
                try:
                    client.recover_snapshot(
                        name,
                        location=f"file://{QDRANT_SNAPSHOT_ROOT}/{name}/{staged_name}",
                        priority=qm.SnapshotPriority.SNAPSHOT,
                        wait=True,
                    )
                finally:
                    staged.unlink(missing_ok=True)
            qdrant_store.reset_client()
            log.append(f"Qdrant collections recovered: {', '.join(collections)}")
        else:
            log.append("no Qdrant snapshot in this bundle: run `rag rebuild-index` after `make up`")

        for label, restored in (trees or {}).items():
            live = {"inbox": settings.inbox_path, "quarantine": settings.quarantine_path,
                    "archive": settings.archive_path}[label]
            if not restored or not restored.is_dir():
                continue
            if _has_files(live):
                log.append(f"{label}: live directory is not empty, left the restored copy in {restored}")
                continue
            shutil.copytree(restored, live, dirs_exist_ok=True)
            log.append(f"{label} restored")

    return log
