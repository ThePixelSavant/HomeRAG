"""The read-only admin dashboard (ADR-031): rag-admin and its views."""

from __future__ import annotations

import re
import sqlite3

import pytest
from starlette.testclient import TestClient

from app import report, report_html
from app.config import settings
from app.domains import Tier
from app.pipeline import state

TOKEN = "t" * 64
VIEWS = ("overview", "documents", "lifecycle", "quarantine", "eval")


@pytest.fixture
def state_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db_path", tmp_path / "rag.db")
    state.init()
    return tmp_path / "rag.db"


@pytest.fixture(autouse=True)
def no_qdrant(monkeypatch):
    """No live Qdrant in unit tests: every count is 7."""
    from app.pipeline import qdrant_store

    monkeypatch.setattr(qdrant_store, "count_points", lambda *a, **k: 7)


@pytest.fixture
def client(state_db, monkeypatch):
    from app import admin_server

    monkeypatch.setattr(settings, "report_token", TOKEN)
    return TestClient(admin_server.app, base_url="http://127.0.0.1:8090")


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _doc(conn, doc_id, uri, *, domain="manuals", tier=Tier.OPEN.value, **kw):
    fields = dict(
        doc_id=doc_id, source_id=f"inbox:{domain}", domain=domain, tier=tier, uri=uri,
        title=uri, content_hash="h", chunk_count=3, extractor="text",
        status=state.INDEXED, last_seen_run="r1",
    )
    fields.update(kw)
    state.upsert_document(conn, **fields)


def _summary(hit1=0.5, hit3=0.75, mrr=0.6, depth=10):
    return {"questions": 4, "hit@1": hit1, "hit@3": hit3, f"mrr@{depth}": mrr, "mean_top3_chars": 900}


def _seed_mixed():
    """Open-tier data next to everything that must never appear: a vault
    domain row, a vault-tier row, a vault source, a vault-only run and a
    quarantine entry under a vault domain."""
    with state.writer() as conn:
        _doc(conn, "o1", "pump.pdf")
        _doc(conn, "o2", "old-pump.pdf", lifecycle="superseded", superseded_by="pump.pdf")
        _doc(conn, "v1", "SECRET-receipt.jpg", domain="receipts")
        _doc(conn, "v2", "SECRET-tier.pdf", domain="manuals", tier=Tier.VAULT.value)
        state.record_source_start(conn, "inbox:manuals", "manuals", "inbox", "r1")
        state.record_source_finish(conn, "inbox:manuals", "ok", docs_seen=2)
        state.record_source_start(conn, "inbox:receipts", "receipts", "inbox", "r1")
        state.record_source_finish(conn, "inbox:receipts", "error", error="SECRET-error IMG_1.jpg")
        state.start_run(conn, "r-open", ["inbox:manuals", "inbox:receipts"])
        state.finish_run(conn, "r-open", "error", "SECRET-error IMG_1.jpg")
        state.start_run(conn, "r-vault", ["inbox:receipts"])
        state.finish_run(conn, "r-vault", "ok")
        state.add_quarantine(conn, path="/q/a.txt", origin_path="/inbox/manuals/a.txt",
                             domain="manuals", rules="aws_key", reason="aws_key: AKIA****")
        state.add_quarantine(conn, path="/q/b.txt", origin_path="/inbox/receipts/SECRET-b.txt",
                             domain="receipts", rules="card_number", reason="card_number: x")


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "short-token"])
def test_everything_is_off_without_a_usable_token(state_db, monkeypatch, token):
    from app import admin_server

    monkeypatch.setattr(settings, "report_token", token)
    c = TestClient(admin_server.app, base_url="http://127.0.0.1:8090")
    assert c.get("/admin/report", headers=_auth(token or "x")).status_code == 404
    assert c.get("/login").status_code == 404
    assert c.post("/login", data={"token": token or ""}).status_code == 404


def test_foreign_host_header_is_refused(client):
    r = client.get("/admin/report", headers={**_auth(), "Host": "evil.example:8090"})
    assert r.status_code == 400


def test_browser_without_a_session_is_sent_to_login(client):
    r = client.get("/admin/report", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_sets_a_derived_cookie_and_logout_clears_it(client):
    bad = client.post("/login", data={"token": "wrong"}, follow_redirects=False)
    assert bad.status_code == 401 and "rag_admin" not in bad.cookies

    r = client.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert TOKEN not in cookie  # the cookie holds a derivative, never the token

    page = client.get("/admin/report")
    assert page.status_code == 200 and "RAG overview" in page.text

    client.post("/logout", follow_redirects=False)
    assert client.get("/admin/report", follow_redirects=False).status_code == 303


def test_forged_cookie_is_refused(client):
    client.cookies.set("rag_admin", "0" * 64)
    assert client.get("/admin/report", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("headers", [_auth("wrong"), {"Authorization": TOKEN},
                                     {"Authorization": "Basic " + TOKEN}])
def test_route_refuses_a_bad_token(client, headers):
    assert client.get("/admin/report", headers=headers).status_code == 401


def test_json_without_credentials_is_401_not_a_redirect(client):
    r = client.get("/admin/report", params={"format": "json"}, follow_redirects=False)
    assert r.status_code == 401


def test_route_is_get_only(client):
    assert client.post("/admin/report", headers=_auth()).status_code == 405


@pytest.mark.parametrize("view", VIEWS)
def test_every_view_renders_with_a_strict_csp(client, view):
    r = client.get("/admin/report", params={"view": view}, headers=_auth())
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["cache-control"] == "no-store"


def test_icon_is_served_so_the_browser_never_asks_for_favicon_ico(client):
    r = client.get("/admin/static/icon.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "img-src 'self'" in r.headers["content-security-policy"]


def test_chart_script_is_served_and_needs_no_session(client):
    r = client.get("/admin/static/report.js")
    assert r.status_code == 200 and "evalchart" in r.text


def test_json_format(client):
    r = client.get("/admin/report", params={"view": "overview", "format": "json"}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["points"]["total"] == 7


def test_unknown_view_and_vault_domain_are_400(client):
    assert client.get("/admin/report", params={"view": "ledger"}, headers=_auth()).status_code == 400
    r = client.get("/admin/report", params={"view": "documents", "domain": "receipts", "format": "json"},
                   headers=_auth())
    assert r.status_code == 400
    assert "receipts" in r.json()["error"]


# --------------------------------------------------------------------------
# Open tier only
# --------------------------------------------------------------------------


@pytest.mark.parametrize("view", VIEWS)
def test_no_view_shows_vault_data(client, view):
    _seed_mixed()
    for fmt in ("json", "html"):
        r = client.get("/admin/report", params={"view": view, "format": fmt, "all": "1"},
                       headers=_auth())
        assert r.status_code == 200
        assert "SECRET" not in r.text
        assert "inbox:receipts" not in r.text
        assert "card_number" not in r.text


def test_overview_counts_open_rows_only(state_db):
    _seed_mixed()
    with state.reader() as conn:
        data = report.overview(conn)
    assert sum(data["documents_by_status"].values()) == 2
    assert data["not_active"] == 1
    assert data["quarantined"] == 1
    assert [s["source_id"] for s in data["sources"]] == ["inbox:manuals"]
    # A run is shown through its open sources; a vault-only run not at all.
    assert [(r["run_id"], r["sources"]) for r in data["runs"]] == [("r-open", ["inbox:manuals"])]
    assert "notes" not in data["runs"][0]


def test_quarantine_shows_rule_names_not_detail(state_db):
    _seed_mixed()
    with state.reader() as conn:
        items = report.quarantine(conn)["items"]
    assert items == [{"id": 1, "path": "/inbox/manuals/a.txt", "domain": "manuals",
                      "rules": ["aws_key"], "detected_at": items[0]["detected_at"]}]


def test_documents_filters(state_db):
    _seed_mixed()
    with state.reader() as conn:
        active = report.document_list(conn)
        everything = report.document_list(conn, include_all=True)
        matched = report.document_list(conn, match="old", include_all=True)
        literal = report.document_list(conn, match="%", include_all=True)
    assert [d["uri"] for d in active["items"]] == ["pump.pdf"]
    assert {d["uri"] for d in everything["items"]} == {"pump.pdf", "old-pump.pdf"}
    assert [d["uri"] for d in matched["items"]] == ["old-pump.pdf"]
    assert literal["items"] == []  # LIKE wildcards are escaped


def test_lifecycle_flags_reviews_due(state_db):
    with state.writer() as conn:
        _doc(conn, "a", "due.md", review_after="2026-01-01")
        _doc(conn, "b", "later.md", review_after="2099-01-01")
    with state.reader() as conn:
        items = {i["uri"]: i for i in report.lifecycle(conn, today="2026-06-01")["items"]}
    assert items["due.md"]["review_due"] and not items["later.md"]["review_due"]


def test_report_works_against_a_missing_database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_db_path", tmp_path / "absent.db")
    with state.reader() as conn:
        for view in VIEWS:
            report_html.render(view, report.build(view, conn))


# --------------------------------------------------------------------------
# Eval history
# --------------------------------------------------------------------------


def test_eval_history_round_trips(state_db):
    with state.writer() as conn:
        state.record_eval(conn, questions_file="q.yaml", questions_hash="a" * 64, depth=10,
                          summary=_summary(), per_question=[{"id": "x", "rank": 2, "top3_chars": 10}])
    with state.reader() as conn:
        (run,) = state.eval_history(conn)
    assert run["hit3"] == 0.75 and run["mrr"] == 0.6
    assert run["per_question"] == [{"id": "x", "rank": 2, "top3_chars": 10}]
    assert run["embed_model"] == settings.embed_model


def test_eval_history_tolerates_a_database_without_the_table(tmp_path):
    path = tmp_path / "old.db"
    sqlite3.connect(path).execute("CREATE TABLE documents (doc_id TEXT)").connection.commit()
    with state.reader(path) as conn:
        assert state.eval_history(conn) == []


def test_eval_compares_ranks_only_within_one_question_set(state_db):
    with state.writer() as conn:
        for h, rank in (("a", 2), ("a", 5), ("b", 1)):
            state.record_eval(conn, questions_file="q.yaml", questions_hash=h * 64, depth=10,
                              summary=_summary(), per_question=[{"id": "x", "rank": rank, "top3_chars": 1}])
    with state.reader() as conn:
        view = report.eval_view(conn)
    assert not view["same_question_set"]
    html = report_html.render("eval", view)
    assert "questions changed" in html
    assert "not compared" in html


def test_cmd_eval_records_unless_told_not_to(state_db, monkeypatch, tmp_path):
    from app import ingest
    from app.pipeline import qdrant_store

    questions = tmp_path / "q.yaml"
    questions.write_text("questions:\n  - {id: x, question: q, uri: pump.pdf, expect: torque}\n")
    monkeypatch.setattr(qdrant_store, "search", lambda *a, **k: [
        {"uri": "pump.pdf", "content": "the torque is 12 Nm"}])
    assert ingest.main(["eval", "--file", str(questions), "--json"]) == 0
    assert ingest.main(["eval", "--file", str(questions), "--json", "--no-record"]) == 0
    with state.reader() as conn:
        runs = state.eval_history(conn)
    assert len(runs) == 1 and runs[0]["hit1"] == 1.0


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------


def test_html_escapes_and_loads_nothing_external(state_db):
    with state.writer() as conn:
        _doc(conn, "o1", "<img src=x onerror=alert(1)>.md")
        state.record_eval(conn, questions_file="q.yaml", questions_hash="a" * 64, depth=10,
                          summary=_summary(), per_question=[{"id": "</script><b>", "rank": None,
                                                             "top3_chars": 1}])
    with state.reader() as conn:
        pages = [report_html.render(v, report.build(v, conn)) for v in VIEWS]
    for page in pages:
        assert "<img src=x" not in page
        assert "</script><b>" not in page
        # The only <link> is the icon, from rag-admin itself.
        assert re.findall(r"<link[^>]*>", page) == [
            '<link rel="icon" type="image/svg+xml" href="/admin/static/icon.svg">']
        assert "http://" not in page and "https://" not in page
        # No inline script (the CSP would block it anyway); only our own file.
        for tag in re.findall(r"<script[^>]*>", page):
            assert 'type="application/json"' in tag or 'src="/admin/static/report.js"' in tag
