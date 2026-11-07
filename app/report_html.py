"""Render the admin report (app/report.py) as HTML pages for rag-admin (ADR-031).

Viewed in a browser at http://127.0.0.1:8090, not inside Open WebUI: Open
WebUI's Functions stay off (~/Dev/LLM), because function code would run in
the container that holds the identity-signing secret and sits on vault-net.

- No external scripts, fonts or stylesheets -- the stack is offline-first, and
  an admin page has no business calling out to a CDN. The one script is
  served by rag-admin itself (CHART_JS), so the CSP can forbid inline script.
- Every value goes through `html.escape`: URIs are file names and the
  quarantine list is, by definition, files nobody has looked at yet.

Charts are inline SVG drawn here, with colours from the dataviz reference
palette (slots 1-3, which validate all-pairs in both modes).
"""

from __future__ import annotations

import json
from html import escape
from urllib.parse import urlencode

from app import documents

REPORT_PATH = "/admin/report"
CHART_JS_PATH = "/admin/static/report.js"
ICON_PATH = "/admin/static/icon.svg"

# Declared on every page so the browser does not fetch /favicon.ico, which the
# CSP would block (and log as an error) on every load.
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<rect x="1" y="8" width="3.5" height="7" rx="1" fill="#2a78d6"/>'
    '<rect x="6.25" y="4" width="3.5" height="11" rx="1" fill="#2a78d6"/>'
    '<rect x="11.5" y="1" width="3.5" height="14" rx="1" fill="#2a78d6"/></svg>'
)

# Crosshair and tooltip for the eval chart. Reads the run data from the
# chart's JSON data block, which CSP does not treat as script.
CHART_JS = """(function () {
  var root = document.getElementById('evalchart');
  if (!root) return;
  var svg = root.querySelector('svg'), tip = root.querySelector('.tip'), xh = svg.querySelector('.xh');
  var data = JSON.parse(root.querySelector('script[type="application/json"]').textContent);
  var vbw = svg.viewBox.baseVal.width;
  function pct(v) { return Math.round(v * 100) + '%'; }
  function show(ev) {
    var box = svg.getBoundingClientRect(), sx = (ev.clientX - box.left) * vbw / box.width;
    var best = data[0];
    data.forEach(function (d) { if (Math.abs(d.x - sx) < Math.abs(best.x - sx)) best = d; });
    xh.setAttribute('x1', best.x); xh.setAttribute('x2', best.x); xh.setAttribute('visibility', 'visible');
    tip.textContent = '';
    [best.at + ' \\u00b7 ' + best.n + ' questions', 'hit@1 ' + pct(best.hit1),
     'hit@3 ' + pct(best.hit3), 'MRR ' + best.mrr.toFixed(3)].forEach(function (t) {
      var div = document.createElement('div'); div.textContent = t; tip.appendChild(div);
    });
    tip.style.display = 'block';
    var px = best.x * box.width / vbw;
    tip.style.left = Math.max(0, Math.min(px + 12, box.width - tip.offsetWidth)) + 'px';
    tip.style.top = '8px';
  }
  var hit = svg.querySelector('.hit');
  hit.addEventListener('mousemove', show);
  hit.addEventListener('mouseleave', function () {
    tip.style.display = 'none'; xh.setAttribute('visibility', 'hidden');
  });
})();
"""

_CSS = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --surface-2: #f3f2ee; --border: #e2e0da;
  --text: #0b0b0b; --text-2: #52514e; --muted: #7a7973;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
  --bar: #2a78d6; --grid: #e7e5df;
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface: #1a1a19; --surface-2: #232321; --border: #383835;
    --text: #f0efec; --text-2: #c3c2b7; --muted: #94938b;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
    --bar: #3987e5; --grid: #2e2e2b;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--surface); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 17px; margin: 0 0 4px; }
h2 { font-size: 14px; margin: 22px 0 8px; color: var(--text-2); font-weight: 600; }
.sub { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; }
.tile { background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.tile .v { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile .k { color: var(--text-2); font-size: 12px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--text-2); font-weight: 600; white-space: nowrap; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
td.uri { word-break: break-all; }
.tag { display: inline-block; font-size: 11px; padding: 0 6px; border-radius: 9px;
  border: 1px solid var(--border); color: var(--text-2); white-space: nowrap; }
.status::before { content: ""; display: inline-block; width: 8px; height: 8px;
  border-radius: 50%; margin-right: 5px; vertical-align: 0; }
.status.good::before { background: var(--good); }
.status.warning::before { background: var(--warning); }
.status.critical::before { background: var(--critical); }
.bars { display: grid; grid-template-columns: max-content 1fr max-content; gap: 4px 10px; align-items: center; }
.bars .track { height: 10px; }
.bars .fill { height: 10px; background: var(--bar); border-radius: 0 4px 4px 0; min-width: 2px; }
.bars .n { font-variant-numeric: tabular-nums; color: var(--text-2); font-size: 12px; }
.empty { color: var(--muted); font-style: italic; }
.chart { position: relative; }
.chart svg { width: 100%; height: auto; display: block; }
.chart text { fill: var(--muted); font-size: 11px; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--text-2); margin: 4px 0 6px; }
.legend i { display: inline-block; width: 12px; height: 2px; vertical-align: middle; margin-right: 5px; }
.tip { position: absolute; pointer-events: none; background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 8px; font-size: 12px; display: none; white-space: nowrap; }
.up { color: var(--good); } .down { color: var(--critical); }
.wrap { max-width: 1100px; margin: 0 auto; }
nav { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin: -4px 0 16px;
  padding-bottom: 10px; border-bottom: 1px solid var(--border); }
nav a { color: var(--text-2); text-decoration: none; padding: 4px 10px; border-radius: 6px; }
nav a:hover { background: var(--surface-2); }
nav a.on { color: var(--text); background: var(--surface-2); font-weight: 600; }
nav form { margin-left: auto; }
button, input, select { font: inherit; color: var(--text); background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; }
button { cursor: pointer; }
form.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
form.filters label { color: var(--text-2); font-size: 12.5px; }
.login { max-width: 360px; margin: 12vh auto; }
.login input { width: 100%; margin: 8px 0; }
.err { color: var(--critical); }
code { font-size: 12.5px; }
"""

_STATUS_CLASS = {"ok": "good", "running": "warning", "error": "critical"}


def _e(value) -> str:
    return escape("" if value is None else str(value))


def _status(value: str | None) -> str:
    cls = _STATUS_CLASS.get(value or "", "warning")
    return f'<span class="status {cls}">{_e(value or "-")}</span>'


def _tile(label: str, value) -> str:
    return f'<div class="tile"><div class="v">{_e(value)}</div><div class="k">{_e(label)}</div></div>'


def _table(headers: list[tuple[str, str]], rows: list[list[str]], empty: str) -> str:
    """`headers` is [(label, css_class)]; cells are already-escaped HTML."""
    if not rows:
        return f'<p class="empty">{_e(empty)}</p>'
    head = "".join(f'<th class="{c}">{_e(h)}</th>' for h, c in headers)
    body = "".join(
        "<tr>" + "".join(f'<td class="{headers[i][1]}">{cell}</td>' for i, cell in enumerate(r)) + "</tr>"
        for r in rows
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _bars(counts: dict[str, int]) -> str:
    if not counts:
        return '<p class="empty">No counts.</p>'
    top = max(counts.values()) or 1
    rows = "".join(
        f'<div>{_e(k)}</div><div class="track"><div class="fill" style="width:{100 * v / top:.1f}%"></div></div>'
        f'<div class="n">{v:,}</div>'
        for k, v in counts.items()
    )
    return f'<div class="bars">{rows}</div>'


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def _date(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


def _overview(d: dict) -> str:
    points = d["points"]
    # The reason is shown under "Chunks by domain"; the tile only says it is missing.
    total = points.get("total", "?") if "error" not in points else "—"
    latest = d.get("latest_eval")
    tiles = "".join([
        _tile("open-tier chunks", f"{total:,}" if isinstance(total, int) else total),
        _tile("documents", f"{sum(d['documents_by_status'].values()):,}"),
        _tile("not active", d["not_active"]),
        _tile("quarantined", d["quarantined"]),
        _tile("rejected page readings", len(d["rejected_transcriptions"])),
        _tile("latest hit@3", _pct(latest["hit3"]) if latest else "no runs"),
    ])
    parts = [
        f'<div class="sub">{_e(d["collection"])} · {_e(d["embed_model"])} ({_e(d["embed_dim"])}d)</div>',
        f'<div class="tiles">{tiles}</div>',
        "<h2>Chunks by domain</h2>",
        f'<p class="empty">Qdrant: {_e(points["error"])}</p>' if "error" in points
        else _bars(points.get("by_domain", {})),
        "<h2>Documents</h2>",
        _table(
            [("status", ""), ("count", "n")],
            [[_e(k), f"{v:,}"] for k, v in sorted(d["documents_by_status"].items())],
            "Nothing indexed yet.",
        ),
    ]
    if d["transcribed_pages"]:
        parts += [
            "<h2>Pages read by the vision model</h2>",
            _table([("outcome", ""), ("pages", "n")],
                   [[_e(k), f"{v:,}"] for k, v in sorted(d["transcribed_pages"].items())], ""),
        ]
    if d["rejected_transcriptions"]:
        parts += [
            "<h2>Rejected page readings</h2>",
            _table([("document", "uri"), ("page", "n"), ("why", "")],
                   [[_e(r["uri"]), _e(r["page"]), _e(r["detail"])] for r in d["rejected_transcriptions"]], ""),
        ]
    parts += [
        "<h2>Sources</h2>",
        _table(
            [("source", ""), ("domain", ""), ("last run", ""), ("finished", ""), ("docs", "n"), ("error", "")],
            [[_e(s["source_id"]), _e(s["domain"]), _status(s["last_status"]), _e(_date(s["last_finished_at"])),
              _e(s["docs_seen"]), _e(s["last_error"] or "")] for s in d["sources"]],
            "No sources have run yet.",
        ),
        "<h2>Recent runs</h2>",
        _table(
            [("started", ""), ("status", ""), ("sources", "")],
            [[_e(_date(r["started_at"])), _status(r["status"]), _e(", ".join(r["sources"]))] for r in d["runs"]],
            "No runs recorded.",
        ),
    ]
    return "".join(parts)


def _documents(d: dict) -> str:
    f = d["filters"]
    scope = ", ".join(x for x in [
        f"domain {f['domain']}" if f["domain"] else "",
        f"matching “{f['match']}”" if f["match"] else "",
        "all lifecycles" if f["all"] else "active only",
    ] if x)
    rows = []
    for r in d["items"]:
        notes = []
        if r["lifecycle"] != documents.ACTIVE:
            notes.append(f'<span class="tag">{_e(r["lifecycle"])}</span>')
        if r["status"] != "indexed":
            notes.append(f'<span class="tag">{_e(r["status"])}</span>')
        # A retracted document is never re-read, so its flag count is frozen
        # and there is nothing to review (same rule as state.flagged_pages).
        if r["flagged_pages"] and r["lifecycle"] not in documents.TOMBSTONED:
            notes.append(f'<span class="tag">{_e(r["flagged_pages"])} page(s) flagged</span>')
        rows.append([_e(r["uri"]), _e(r["domain"]), f'{r["chunk_count"]:,}',
                     _e((r["indexed_at"] or "")[:10]), " ".join(notes)])
    options = "".join(
        f'<option value="{_e(x)}"{" selected" if x == f["domain"] else ""}>{_e(x)}</option>'
        for x in d["domains"]
    )
    form = (
        f'<form class="filters" method="get" action="{REPORT_PATH}">'
        '<input type="hidden" name="view" value="documents">'
        f'<label>domain <select name="domain"><option value="">all</option>{options}</select></label>'
        f'<label>name contains <input name="match" value="{_e(f["match"] or "")}" size="18"></label>'
        f'<label><input type="checkbox" name="all" value="1"{" checked" if f["all"] else ""}> '
        "include superseded / stale / retracted</label>"
        "<button>Filter</button></form>"
    )
    return (
        form
        + f'<div class="sub">{len(d["items"])} document(s), {d["total_chunks"]:,} chunks · {_e(scope)}</div>'
        + _table([("document", "uri"), ("domain", ""), ("chunks", "n"), ("indexed", ""), ("", "")],
                 rows, "No documents match.")
    )


def _lifecycle(d: dict) -> str:
    rows = []
    for r in d["items"]:
        if r["lifecycle"] == documents.SUPERSEDED and r["superseded_by"]:
            detail = f"→ {_e(r['superseded_by'])}"
        elif r["review_due"]:
            detail = f'<span class="status warning">review due {_e(r["review_after"])}</span>'
        elif r["lifecycle"] == documents.ACTIVE and r["review_after"]:
            detail = f"review on {_e(r['review_after'])}"
        else:
            detail = _e(r["lifecycle_reason"] or "")
        rows.append([_e(r["lifecycle"]), _e(r["uri"]), _e(r["domain"]), detail])
    counts = " · ".join(f"{_e(k)} {v}" for k, v in sorted(d["counts"].items()))
    return (
        f'<div class="sub">{counts or "no documents"}</div>'
        + _table([("state", ""), ("document", "uri"), ("domain", ""), ("detail", "")],
                 rows, "Every document is active, with no reviews scheduled.")
        + '<p class="sub">Changes are made at the terminal: rag supersede / stale / retract / restore.</p>'
    )


def _quarantine(d: dict) -> str:
    return (
        _table([("file", "uri"), ("domain", ""), ("rules", ""), ("held since", "")],
               [[_e(r["path"]), _e(r["domain"]),
                 " ".join(f'<span class="tag">{_e(x)}</span>' for x in r["rules"]),
                 _e(_date(r["detected_at"]))] for r in d["items"]],
               "Nothing is held in quarantine.")
        + '<p class="sub">Review at the terminal: rag review-quarantine.</p>'
    )


_SERIES = [("hit1", "hit@1", "--series-1"), ("hit3", "hit@3", "--series-2"), ("mrr", "MRR", "--series-3")]


def _eval_chart(runs: list[dict]) -> str:
    """hit@1, hit@3 and MRR over recorded runs, on one 0-1 axis."""
    w, h, left, right, top, bottom = 640, 220, 34, 54, 12, 26
    pw, ph = w - left - right, h - top - bottom
    n = len(runs)

    def x(i: int) -> float:
        return left + (pw * i / (n - 1) if n > 1 else pw / 2)

    def y(v: float) -> float:
        return top + ph * (1 - v)

    svg = []
    for t in (0, 0.25, 0.5, 0.75, 1):
        svg.append(f'<line x1="{left}" x2="{left + pw}" y1="{y(t):.1f}" y2="{y(t):.1f}" '
                   f'stroke="var(--grid)" stroke-width="1"/>')
        svg.append(f'<text x="{left - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{t:.0%}</text>')

    # Where the question set or the embedding model changed, a step is not a
    # retrieval change. Mark it rather than let it read as one.
    for i in range(1, n):
        a, b = runs[i - 1], runs[i]
        why = []
        if a["questions_hash"] != b["questions_hash"]:
            why.append("questions changed")
        if a["embed_model"] != b["embed_model"]:
            why.append("model changed")
        if a["chunk_target_tokens"] != b["chunk_target_tokens"]:
            why.append("chunking changed")
        if why:
            xm = (x(i - 1) + x(i)) / 2
            svg.append(f'<line x1="{xm:.1f}" x2="{xm:.1f}" y1="{top}" y2="{top + ph}" '
                       f'stroke="var(--muted)" stroke-dasharray="3 3" stroke-width="1"/>')
            svg.append(f'<text x="{xm + 3:.1f}" y="{top + 10}">{_e(", ".join(why))}</text>')

    for key, label, var in _SERIES:
        pts = " ".join(f"{x(i):.1f},{y(r[key]):.1f}" for i, r in enumerate(runs))
        if n > 1:
            svg.append(f'<polyline points="{pts}" fill="none" stroke="var({var})" stroke-width="2" '
                       'stroke-linejoin="round" stroke-linecap="round"/>')
        for i, r in enumerate(runs):
            svg.append(f'<circle cx="{x(i):.1f}" cy="{y(r[key]):.1f}" r="4" fill="var({var})" '
                       'stroke="var(--surface)" stroke-width="2"/>')
    # Direct labels at the latest point, nudged apart so they cannot collide.
    last = runs[-1]
    placed: list[float] = []
    for key, label, var in sorted(_SERIES, key=lambda s: -last[s[0]]):
        ly = y(last[key]) + 4
        for p in placed:
            if abs(ly - p) < 12:
                ly = p + 12
        placed.append(ly)
        svg.append(f'<text x="{x(n - 1) + 8:.1f}" y="{ly:.1f}" style="fill:var(--text-2)">{_e(label)}</text>')

    svg.append(f'<text x="{left}" y="{h - 6}">{_e(_date(runs[0]["run_at"]))}</text>')
    if n > 1:
        svg.append(f'<text x="{left + pw}" y="{h - 6}" text-anchor="end">{_e(_date(last["run_at"]))}</text>')
    svg.append(f'<line class="xh" x1="0" x2="0" y1="{top}" y2="{top + ph}" stroke="var(--muted)" '
               'stroke-width="1" visibility="hidden"/>')
    svg.append(f'<rect class="hit" x="{left}" y="{top}" width="{pw}" height="{ph}" fill="transparent"/>')

    data = [
        {"x": round(x(i), 1), "at": _date(r["run_at"]), "hit1": r["hit1"], "hit3": r["hit3"],
         "mrr": r["mrr"], "n": r["questions"]}
        for i, r in enumerate(runs)
    ]
    # `</` escaped so a value can never close the script element early.
    payload = json.dumps(data).replace("</", "<\\/")
    legend = "".join(f'<span><i style="background:var({v})"></i>{_e(l)}</span>' for _, l, v in _SERIES)
    return f"""
<div class="legend">{legend}</div>
<div class="chart" id="evalchart">
  <svg viewBox="0 0 {w} {h}" role="img" aria-label="hit@1, hit@3 and MRR across eval runs">{''.join(svg)}</svg>
  <div class="tip"></div>
  <script type="application/json">{payload}</script>
</div>
<script src="{CHART_JS_PATH}" defer></script>"""


def _rank(r) -> str:
    return "&gt;depth" if r is None else str(r)


def _eval(d: dict) -> str:
    runs = d["runs"]
    if not runs:
        return '<p class="empty">No eval runs recorded yet. Run <code>rag eval</code> at the terminal.</p>'
    latest = d["latest"]
    tiles = "".join([
        _tile("hit@1", _pct(latest["hit1"])),
        _tile("hit@3", _pct(latest["hit3"])),
        _tile(f"MRR@{latest['depth']}", f"{latest['mrr']:.3f}"),
        _tile("mean top-3 chars", f"{latest['mean_top3_chars']:,}"),
    ])
    rows = []
    for q in d["comparison"]:
        prev, cur = q["previous_rank"], q["rank"]
        change = ""
        if d["same_question_set"] and prev != cur:
            better = cur is not None and (prev is None or cur < prev)
            change = (f'<span class="{"up" if better else "down"}">'
                      f'{"▲ improved" if better else "▼ worse"} from {_rank(prev)}</span>')
        rows.append([_e(q["id"]), _rank(cur), change, f'{q["top3_chars"]:,}'])
    history = [
        [_e(_date(r["run_at"])), str(r["questions"]), _pct(r["hit1"]), _pct(r["hit3"]),
         f'{r["mrr"]:.3f}', _e(r["embed_model"]), _e(r["questions_hash"][:8])]
        for r in reversed(runs)
    ]
    return (
        f'<div class="sub">Latest run {_e(_date(latest["run_at"]))} · {latest["questions"]} questions · '
        f'{_e(latest["questions_file"])}</div>'
        f'<div class="tiles">{tiles}</div>'
        "<h2>Over time</h2>" + _eval_chart(runs)
        + "<h2>Latest run by question</h2>"
        + ("" if d["same_question_set"] or len(runs) < 2 else
           '<p class="sub">The question set changed since the previous run, so ranks are not compared.</p>')
        + _table([("question", ""), ("rank", "n"), ("vs previous", ""), ("top-3 chars", "n")], rows, "")
        + "<h2>All runs</h2>"
        + _table([("run", ""), ("questions", "n"), ("hit@1", "n"), ("hit@3", "n"), ("MRR", "n"),
                  ("model", ""), ("question set", "")], history, "")
    )


_RENDERERS = {
    "overview": ("RAG overview", _overview),
    "documents": ("Documents", _documents),
    "lifecycle": ("Lifecycle", _lifecycle),
    "quarantine": ("Quarantine", _quarantine),
    "eval": ("Retrieval eval", _eval),
}


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_e(title)} · RAG Admin</title>"
        f'<link rel="icon" type="image/svg+xml" href="{ICON_PATH}">'
        f"<style>{_CSS}</style></head>"
        f"<body><div class=\"wrap\">{body}</div></body></html>"
    )


def _nav(current: str | None) -> str:
    links = "".join(
        f'<a href="{REPORT_PATH}?{urlencode({"view": v})}"{" class=on" if v == current else ""}>'
        f"{_e(label)}</a>"
        for v, (label, _) in _RENDERERS.items()
    )
    return (
        f"<nav>{links}"
        '<form method="post" action="/logout"><button>Log out</button></form></nav>'
    )


def render(view: str, data: dict) -> str:
    title, fn = _RENDERERS[view]
    return _page(title, f"{_nav(view)}<h1>{_e(title)}</h1>{fn(data)}")


def render_error(message: str) -> str:
    return _page("Error", f'{_nav(None)}<p class="err">{_e(message)}</p>')


def render_login(error: str | None = None) -> str:
    err = f'<p class="err">{_e(error)}</p>' if error else ""
    return _page("Log in", (
        '<div class="login"><h1>RAG Admin</h1>'
        '<p class="sub">Read-only. Paste REPORT_TOKEN from ~/Dev/RAG/.env.</p>'
        f"{err}"
        '<form method="post" action="/login">'
        '<input type="password" name="token" autocomplete="current-password" '
        'aria-label="token" autofocus required>'
        "<button>Log in</button></form></div>"
    ))
