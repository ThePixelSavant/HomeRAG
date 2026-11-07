"""Read-only admin dashboard (ADR-031). Runs as the `rag-admin` container.

Open it in a browser at http://127.0.0.1:8090 (host loopback only). It is
deliberately NOT inside Open WebUI: a pipe there needs ENABLE_FUNCTIONS, and
function code runs in the container that holds the identity-signing secret
and sits on vault-net. ~/Dev/LLM keeps Functions off for that reason.

    GET  /admin/report?view=overview|documents|lifecycle|quarantine|eval
                       [&domain=][&match=][&all=1][&format=json]
    GET  /login, POST /login, POST /logout
    GET  /admin/static/report.js, GET /healthz

What keeps it small:

- **Read-only.** rag.db through `state.reader()` (the mount is `:ro` anyway),
  Qdrant with the read-only key. Lifecycle changes, uploads and quarantine
  decisions stay at the terminal.
- **Open tier only.** Enforced in app/report.py; nothing here imports
  app.vault, and the container is not on vault-net.
- **One secret, REPORT_TOKEN.** A browser logs in with it once and gets a
  cookie derived from it (rotating the token logs everyone out); curl sends it
  as a bearer. Not Open WebUI's identity assertion: every tool server receives
  that, so any of them could replay it.
- **Off unless configured.** With no token, or one shorter than
  MIN_TOKEN_LENGTH, every page answers 404.
- **Host header pinned.** Only 127.0.0.1 / localhost, so a DNS-rebinding page
  cannot talk to it under its own name -- and would not hold the cookie anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from app import report, report_html
from app.config import settings
from app.pipeline import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

COOKIE = "rag_admin"
MIN_TOKEN_LENGTH = 32

# No inline script, nothing off-origin, never framed. Inline styles are
# allowed for the bar widths.
_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _token() -> str | None:
    token = settings.report_token or ""
    return token if len(token) >= MIN_TOKEN_LENGTH else None


def _session_value(token: str) -> str:
    # Derived, not the token itself, so the cookie jar never holds the secret.
    return hmac.new(token.encode(), b"rag-admin-session-v1", hashlib.sha256).hexdigest()


def _authorised(request: Request, token: str) -> bool:
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and supplied:
        return hmac.compare_digest(supplied.encode(), token.encode())
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie.encode(), _session_value(token).encode())


def _not_found() -> Response:
    return PlainTextResponse("Not Found", status_code=404)


def _html(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers=_HEADERS)


def _build(view: str, params: dict) -> dict:
    with state.reader() as conn:
        return report.build(view, conn, params)


async def admin_report(request: Request) -> Response:
    token = _token()
    if token is None:
        return _not_found()
    q = request.query_params
    as_json = q.get("format") == "json"
    if not _authorised(request, token):
        if as_json or "authorization" in request.headers:
            return PlainTextResponse(
                "Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        return RedirectResponse("/login", status_code=303)

    view = q.get("view", "overview")
    params = {"domain": q.get("domain"), "match": q.get("match"), "all": q.get("all") in ("1", "true")}
    try:
        data = await run_in_threadpool(_build, view, params)
    except ValueError as exc:
        # Unknown view or domain: the message names the valid ones.
        if as_json:
            return JSONResponse({"error": str(exc)}, status_code=400, headers={"Cache-Control": "no-store"})
        return _html(report_html.render_error(str(exc)), status_code=400)

    if as_json:
        return JSONResponse(data, headers={"Cache-Control": "no-store"})
    return _html(report_html.render(view, data))


async def login_form(request: Request) -> Response:
    if _token() is None:
        return _not_found()
    return _html(report_html.render_login())


async def login(request: Request) -> Response:
    token = _token()
    if token is None:
        return _not_found()
    form = await request.form()
    supplied = str(form.get("token", ""))
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        logger.warning("rejected a login from %s", request.client.host if request.client else "?")
        return _html(report_html.render_login("That is not the token."), status_code=401)
    response = RedirectResponse(report_html.REPORT_PATH, status_code=303)
    # Not `secure`: served over plain http on loopback, where nothing crosses
    # a wire. A session cookie: closing the browser logs out.
    response.set_cookie(COOKIE, _session_value(token), httponly=True, samesite="strict", path="/")
    return response


async def logout(_: Request) -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


async def root(_: Request) -> Response:
    return RedirectResponse(report_html.REPORT_PATH, status_code=303)


async def chart_js(_: Request) -> Response:
    return Response(report_html.CHART_JS, media_type="text/javascript", headers=_HEADERS)


async def icon(_: Request) -> Response:
    return Response(report_html.ICON_SVG, media_type="image/svg+xml", headers=_HEADERS)


async def health(_: Request) -> Response:
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/", root, methods=["GET"]),
        Route(report_html.REPORT_PATH, admin_report, methods=["GET"]),
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route(report_html.CHART_JS_PATH, chart_js, methods=["GET"]),
        Route(report_html.ICON_PATH, icon, methods=["GET"]),
        Route("/healthz", health, methods=["GET"]),
    ],
    middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])],
)


def main() -> None:
    import uvicorn

    if _token() is None:
        logger.warning(
            "REPORT_TOKEN is unset or shorter than %d characters: every page will answer 404",
            MIN_TOKEN_LENGTH,
        )
    uvicorn.run(app, host=settings.admin_host, port=settings.admin_port, log_level="info")


if __name__ == "__main__":
    main()
