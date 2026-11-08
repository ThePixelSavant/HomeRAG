"""scripts/rag-backup against a stand-in restic (ADR-032).

The stand-in keeps a repo's snapshot list in a JSON file, so a test can play a
hostile NAS by editing it between runs. What is under test is the script's own
logic: config parsing, the reachability probe, and rollback detection.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rag-backup"

FAKE_RESTIC = r'''#!/usr/bin/env python3
import json, os, secrets, sys
from pathlib import Path
state = Path(os.environ["FAKE_RESTIC_STATE"])
snaps_file = state / "snaps.json"
snaps = json.loads(snaps_file.read_text()) if snaps_file.exists() else []
with open(state / "calls.log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
takes_value = {"--repo", "--password-file", "--retry-lock", "--tag", "--host", "--group-by",
               "--keep-daily", "--keep-weekly", "--keep-monthly", "--target"}
args, i = [], 1
while i < len(sys.argv):
    a = sys.argv[i]
    i += 2 if a in takes_value else 1
    if not a.startswith("-"):
        args.append(a)
cmd = args[0]
if cmd == "snapshots":
    print(json.dumps(snaps))
elif cmd == "backup":
    sid = secrets.token_hex(32)
    snaps.append({"id": sid, "short_id": sid[:8]})
    snaps_file.write_text(json.dumps(snaps))
    print(json.dumps({"message_type": "summary", "snapshot_id": sid, "data_added": 2048}))
elif cmd == "check":
    sys.exit(int(os.environ.get("FAKE_CHECK_RC", "0")))
'''


@pytest.fixture
def env(tmp_path):
    fake = tmp_path / "restic"
    fake.write_text(FAKE_RESTIC)
    fake.chmod(0o755)
    data = tmp_path / "data"
    bundle = data / "backups" / "rag-20261108T000000Z"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text("{}")
    (data / "inbox").mkdir()
    conf = tmp_path / "conf"
    (conf / "repos.d").mkdir(parents=True)
    password = conf / "restic-password"
    password.write_text("x" * 44)
    password.chmod(0o600)
    rstate = tmp_path / "fake-state"
    rstate.mkdir()
    environ = {
        **os.environ,
        "RESTIC": str(fake), "RAG_BACKUP_CONF": str(conf), "RAG_BACKUP_STATE": str(tmp_path / "state"),
        "RAG_BACKUP_DATA": str(data), "RAG_BACKUP_NO_RECORD": "1", "FAKE_RESTIC_STATE": str(rstate),
    }

    def repo(name="nas", **extra):
        lines = [f"RESTIC_REPOSITORY={tmp_path}/repo-{name}   # a comment",
                 "RESTIC_PASSWORD_FILE=" + str(password)]
        lines += [f"{k}={v}" for k, v in extra.items()]
        path = conf / "repos.d" / f"{name}.env"
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
        return path

    def run(*args, **kw):
        return subprocess.run([str(SCRIPT), *args], env={**environ, **kw}, capture_output=True,
                              text=True, timeout=60)

    class Env:
        pass

    e = Env()
    e.repo, e.run, e.snaps, e.state = repo, run, rstate / "snaps.json", tmp_path / "state"
    return e


pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="the script needs jq")


def test_no_repos_is_not_an_error(env):
    out = env.run("offsite")
    assert out.returncode == 0
    assert "local bundle is the only copy" in out.stdout


def test_ok_run_records_a_baseline(env):
    env.repo()
    out = env.run("offsite")
    assert out.returncode == 0, out.stderr
    assert "nas: ok" in out.stdout
    snap_id, count = (env.state / "nas.last").read_text().split()
    assert count == "1" and json.loads(env.snaps.read_text())[0]["id"] == snap_id


def test_unreachable_nas_is_skipped_fast(env):
    env.repo(RAG_BACKUP_PROBE="127.0.0.1:1")
    start = time.monotonic()
    out = env.run("offsite")
    assert out.returncode == 0
    assert "skipped (127.0.0.1:1 not answering)" in out.stdout
    assert time.monotonic() - start < 10
    assert not env.snaps.exists()           # restic never touched the path


def test_unmounted_usb_is_skipped(env, tmp_path):
    env.repo("usb", RAG_BACKUP_REQUIRE_PATH=str(tmp_path / "not-a-mount"))
    out = env.run("offsite")
    assert out.returncode == 0 and "usb: skipped" in out.stdout


def test_rollback_is_reported_and_sticks_until_acknowledged(env):
    env.repo()
    assert env.run("offsite").returncode == 0
    assert env.run("offsite").returncode == 0
    # The NAS quietly deletes the newest snapshot.
    snaps = json.loads(env.snaps.read_text())
    env.snaps.write_text(json.dumps(snaps[:-1]))

    out = env.run("offsite")
    assert out.returncode != 0
    assert "ROLLBACK" in out.stdout and "nas: rollback" in out.stdout
    # Still alarmed on the next, otherwise clean, run.
    out = env.run("offsite")
    assert out.returncode != 0 and "nas: rollback" in out.stdout

    assert "acknowledged" in env.run("ack", "nas").stdout
    out = env.run("offsite")
    assert out.returncode == 0 and "nas: ok" in out.stdout


def test_wiped_repo_is_a_rollback(env):
    env.repo()
    env.run("offsite")
    env.snaps.write_text("[]")
    out = env.run("offsite")
    assert out.returncode != 0 and "ROLLBACK" in out.stdout


def test_failed_read_check_fails_the_run(env):
    env.repo()
    out = env.run("offsite", FAKE_CHECK_RC="1")
    assert out.returncode != 0 and "check --read-data FAILED" in out.stdout


def test_config_readable_by_others_is_refused(env):
    env.repo().chmod(0o644)
    out = env.run("offsite")
    assert out.returncode != 0 and "readable by others" in out.stderr


def test_unknown_key_is_refused(env):
    path = env.repo()
    path.write_text(path.read_text() + "RESTIC_PASSWORD=hunter2\n")
    out = env.run("offsite")
    assert out.returncode != 0 and "unknown key RESTIC_PASSWORD" in out.stderr


def test_ambient_restic_variables_are_ignored(env, tmp_path):
    env.repo()
    out = env.run("offsite", RESTIC_REPOSITORY="/elsewhere", RESTIC_PASSWORD="leaked")
    assert out.returncode == 0
    calls = (tmp_path / "fake-state" / "calls.log").read_text()
    assert "/elsewhere" not in calls and f"--repo {tmp_path}/repo-nas" in calls


def test_forget_groups_by_tag(env, tmp_path):
    """Bundle paths change daily; grouping by path would never forget anything."""
    env.repo()
    env.run("offsite")
    calls = (tmp_path / "fake-state" / "calls.log").read_text()
    forget = next(line for line in calls.splitlines() if " forget " in f" {line} ")
    assert "--group-by tags" in forget and "--keep-daily 7" in forget
