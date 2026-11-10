"""Tenants of a shared Qdrant (ADR-035).

Qdrant enforces a token's scope, and that was verified against a real 1.20
server (see the ADR); an in-memory client enforces nothing. So these tests
check what this code controls: what a token claims, what the admin commands
create, and which path the client takes under a tenant token.
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app import ingest, tenants
from app.config import settings
from app.pipeline import embedder, qdrant_store, state

MAIN_KEY = "m" * 64


def _scoped(collections=("alice", "alice__meta"), access="rw", **extra) -> str:
    claims = {"exp": int(time.time()) + 3600,
              "access": [{"collection": c, "access": access} for c in collections], **extra}
    return jwt.encode(claims, MAIN_KEY, algorithm="HS256")


@pytest.fixture
def qdrant(monkeypatch):
    client = QdrantClient(":memory:")
    monkeypatch.setattr(qdrant_store, "_client", client)
    monkeypatch.setattr(settings, "qdrant_api_key", MAIN_KEY)
    monkeypatch.setattr(settings, "collection_name", "docs")
    return client


def _decode(token: str) -> dict:
    return jwt.decode(token, MAIN_KEY, algorithms=["HS256"])


# --------------------------------------------------------------------------
# Reading the configured key
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, "", MAIN_KEY, "a.b.c", "x.!!!.y"])
def test_a_plain_or_garbled_key_is_not_a_token(key):
    assert qdrant_store.token_claims(key) is None
    assert not qdrant_store.is_tenant(key)


def test_a_scoped_token_is_a_tenant_and_a_global_one_is_not():
    assert qdrant_store.is_tenant(_scoped())
    assert not qdrant_store.is_tenant(jwt.encode({"access": "m"}, MAIN_KEY, algorithm="HS256"))
    assert qdrant_store.token_claims(_scoped())["access"][0]["collection"] == "alice"


# --------------------------------------------------------------------------
# Admin commands
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a", "Alice", "1alice", "al ice", "al__ice", "docs",
                                  tenants.REGISTRY, "x" * 49])
def test_bad_or_reserved_names_are_refused(qdrant, name):
    with pytest.raises(tenants.TenantError):
        tenants.validate_name(name)


def test_create_makes_a_fingerprinted_pair_and_registers_it(qdrant):
    tenants.create("alice")
    assert qdrant.collection_exists("alice")
    info = qdrant.get_collection("alice")
    assert info.config.params.vectors[qdrant_store.DENSE].size == settings.embed_dim
    (meta,) = qdrant.retrieve("alice__meta", ids=[0], with_payload=True)
    assert meta.payload == embedder.model_fingerprint()
    assert tenants._row(qdrant, "alice")["tenant"] == "alice"
    with pytest.raises(tenants.TenantError, match="already exists"):
        tenants.create("alice")


def test_a_token_names_only_its_tenants_pair_and_always_expires(qdrant):
    tenants.create("alice")
    token, expires = tenants.issue_token("alice", access="write", days=30)
    claims = _decode(token)
    assert claims["access"] == [{"collection": "alice", "access": "rw"},
                                {"collection": "alice__meta", "access": "rw"}]
    assert claims["exp"] == expires and 29 * 86400 < expires - time.time() <= 30 * 86400
    gen = tenants._row(qdrant, "alice")["generation"]
    assert claims["value_exists"] == {
        "collection": tenants.REGISTRY,
        "matches": [{"key": "tenant", "value": "alice"}, {"key": "generation", "value": gen}],
    }
    read, _ = tenants.issue_token("alice", access="read")
    assert {a["access"] for a in _decode(read)["access"]} == {"r"}


@pytest.mark.parametrize("kwargs", [{"days": 0}, {"days": tenants.MAX_TOKEN_DAYS + 1},
                                    {"access": "manage"}])
def test_tokens_outside_the_limits_are_refused(qdrant, kwargs):
    tenants.create("alice")
    with pytest.raises(tenants.TenantError):
        tenants.issue_token("alice", **kwargs)


def test_no_token_for_a_tenant_that_does_not_exist(qdrant):
    with pytest.raises(tenants.TenantError, match="No tenant"):
        tenants.issue_token("bob")


def test_revoking_moves_the_generation_so_old_tokens_stop_matching(qdrant):
    tenants.create("alice")
    old, _ = tenants.issue_token("alice")
    tenants.revoke("alice")
    tenants.revoke("alice")  # twice in one second still moves it
    new, _ = tenants.issue_token("alice")
    old_gen = _decode(old)["value_exists"]["matches"][1]["value"]
    new_gen = _decode(new)["value_exists"]["matches"][1]["value"]
    assert new_gen == tenants._row(qdrant, "alice")["generation"]
    assert new_gen > old_gen


def test_a_lost_registry_row_comes_back_with_a_new_generation(qdrant):
    tenants.create("alice")
    old, _ = tenants.issue_token("alice")
    qdrant.delete_collection(tenants.REGISTRY)
    tenants.revoke("alice")
    old_gen = _decode(old)["value_exists"]["matches"][1]["value"]
    assert tenants._row(qdrant, "alice")["generation"] > old_gen


def test_list_and_delete(qdrant):
    tenants.create("alice")
    tenants.create("bob")
    assert [r["tenant"] for r in tenants.list_tenants()] == ["alice", "bob"]
    assert tenants.list_tenants()[0]["points"] == 0
    assert set(tenants.delete("alice")) == {"alice", "alice__meta", f"{tenants.REGISTRY} row"}
    assert not qdrant.collection_exists("alice")
    assert [r["tenant"] for r in tenants.list_tenants()] == ["bob"]
    with pytest.raises(tenants.TenantError):
        tenants.delete("alice")


def test_admin_commands_refuse_under_a_token(qdrant, monkeypatch):
    # A tenant's worker must not even try: its token cannot sign anything.
    monkeypatch.setattr(settings, "qdrant_api_key", _scoped())
    with pytest.raises(tenants.TenantError, match="not the server's main key"):
        tenants.create("carol")


# --------------------------------------------------------------------------
# The client under a tenant token
# --------------------------------------------------------------------------


@pytest.fixture
def as_alice(qdrant, monkeypatch):
    tenants.create("alice")
    monkeypatch.setattr(settings, "collection_name", "alice")
    monkeypatch.setattr(settings, "qdrant_api_key", _scoped())
    return qdrant


def _point(i: int) -> qm.PointStruct:
    return qdrant_store.make_point(
        source_id="s", doc_id="d", chunk_index=i, dense=[0.1] * settings.embed_dim,
        sparse=SimpleNamespace(indices=[1], values=[1.0]), payload={"doc_id": "d"},
    )


def test_a_tenant_cannot_create_its_collection_and_is_told_who_can(qdrant, monkeypatch):
    monkeypatch.setattr(settings, "collection_name", "alice")
    monkeypatch.setattr(settings, "qdrant_api_key", _scoped())
    with pytest.raises(qdrant_store.TenantScopeError, match="tenant-create alice"):
        qdrant_store.init_collection()
    assert not qdrant.collection_exists("alice")


def test_a_tenant_clears_rather_than_drops(as_alice):
    qdrant_store.upsert_chunks([_point(0), _point(1)])
    with pytest.raises(qdrant_store.TenantScopeError):
        qdrant_store.drop_collection()
    qdrant_store.clear_collection()
    assert qdrant_store.count_points() == 0
    assert as_alice.collection_exists("alice")
    qdrant_store.verify_fingerprint()  # still there


def test_an_unfingerprinted_empty_collection_gets_one(qdrant):
    qdrant.create_collection("docs", vectors_config={
        qdrant_store.DENSE: qm.VectorParams(size=settings.embed_dim, distance=qm.Distance.COSINE)})
    qdrant_store.init_collection()
    assert qdrant_store._read_fingerprint() == embedder.model_fingerprint()


def test_an_unfingerprinted_full_collection_is_not_guessed_at(qdrant):
    qdrant.create_collection("docs", vectors_config={
        qdrant_store.DENSE: qm.VectorParams(size=settings.embed_dim, distance=qm.Distance.COSINE)},
        sparse_vectors_config={qdrant_store.SPARSE: qm.SparseVectorParams()})
    qdrant_store.upsert_chunks([_point(0)])
    qdrant_store.init_collection()
    # Points of unknown provenance: recording today's model would bless them.
    assert qdrant_store._read_fingerprint() is None


@pytest.fixture
def rebuild(as_alice, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db_path", tmp_path / "rag.db")
    state.init()
    runs = []
    monkeypatch.setattr(ingest, "_run", lambda sources, **kw: runs.append(sources) or 0)
    monkeypatch.setattr(ingest, "all_sources", lambda: [])
    return runs


def test_rebuild_index_under_a_tenant_token_clears_and_re_embeds(rebuild, as_alice):
    qdrant_store.upsert_chunks([_point(0)])
    assert ingest.main(["rebuild-index", "--yes"]) == 0
    assert qdrant_store.count_points() == 0 and rebuild == [[]]


def test_rebuild_index_will_not_clear_a_tenant_index_for_a_model_it_cannot_hold(
        rebuild, as_alice, monkeypatch):
    qdrant_store.upsert_chunks([_point(0)])
    monkeypatch.setattr(settings, "embed_model", "BAAI/bge-base-en-v1.5")
    assert ingest.main(["rebuild-index", "--yes"]) == 1
    assert qdrant_store.count_points() == 1 and rebuild == []


# --------------------------------------------------------------------------
# Snapshot download
# --------------------------------------------------------------------------


def _serve(monkeypatch, status=200, body=b"snapshot-bytes"):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, content=body)

    monkeypatch.setattr(qdrant_store, "_http", lambda: httpx.Client(
        base_url="http://q:6333", headers={"api-key": "k"}, transport=httpx.MockTransport(handler)))
    return seen


def test_download_checks_qdrants_checksum(monkeypatch, tmp_path):
    seen = _serve(monkeypatch)
    snap = SimpleNamespace(name="s 1.snapshot", checksum=hashlib.sha256(b"snapshot-bytes").hexdigest())
    qdrant_store.download_snapshot("alice", snap, tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"snapshot-bytes"
    assert seen[0].url.raw_path == b"/collections/alice/snapshots/s%201.snapshot"
    assert seen[0].headers["api-key"] == "k"


@pytest.mark.parametrize("status,checksum", [(200, "0" * 64), (403, None)])
def test_a_bad_download_leaves_nothing_behind(monkeypatch, tmp_path, status, checksum):
    _serve(monkeypatch, status=status)
    with pytest.raises((qdrant_store.SnapshotChecksumError, httpx.HTTPStatusError)):
        qdrant_store.download_snapshot("alice", SimpleNamespace(name="s", checksum=checksum),
                                       tmp_path / "out")
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def _row(rows, label):
    return next(r for r in rows if r[1] == label)


def test_doctor_reports_a_tenant_token_and_its_expiry(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")
    monkeypatch.setattr(settings, "qdrant_api_key", _scoped())
    status, _, detail = _row(ingest._qdrant_access_rows(), "qdrant access")
    assert status == ingest.WARN  # an hour left
    assert "alice:rw" in detail
    status, _, detail = _row(ingest._qdrant_access_rows(now=time.time() + 7200), "qdrant access")
    assert status == ingest.BAD and "EXPIRED" in detail


def test_doctor_warns_of_a_token_that_never_expires(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_api_key", jwt.encode(
        {"access": [{"collection": "alice", "access": "r"}]}, MAIN_KEY, algorithm="HS256"))
    assert _row(ingest._qdrant_access_rows(), "qdrant access")[0] == ingest.WARN


@pytest.mark.parametrize("url,warned", [("http://qdrant:6333", False), ("https://q.example:6333", False),
                                        ("http://10.0.0.5:6333", True)])
def test_doctor_warns_when_a_key_crosses_a_network_in_clear(monkeypatch, url, warned):
    monkeypatch.setattr(settings, "qdrant_url", url)
    monkeypatch.setattr(settings, "qdrant_api_key", MAIN_KEY)
    labels = [r[1] for r in ingest._qdrant_access_rows()]
    assert ("qdrant transport" in labels) is warned
