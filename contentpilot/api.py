"""ContentPilot API + Android share target. Runs next to Streamlit (uvicorn, localhost only);
`tailscale serve` puts both on one private HTTPS host.

    uvicorn contentpilot.api:app --host 127.0.0.1 --port 8601

Routes
    POST /api/ingest     {"urls": [...], "intent": "..."}  header X-Ingest-Token  -> per-URL result
    GET  /api/health     ok + DB row counts
    GET  /share          Android Web Share Target page (?url=&text=&title=)
    POST /share          the page's Add button (same-origin, from a tailnet user)
    GET  /manifest.json  installable web app with share_target -> /share
    GET  /sw.js          minimal service worker
    GET  /pwa/<icon>.png app icons
"""
import html
import re
import secrets
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from contentpilot import config
from contentpilot.copilot import service

load_dotenv()

WEB_DIR = Path(__file__).parent / "web"
ICONS = {"icon-192.png", "icon-512.png", "icon-maskable-512.png"}
_MOUNTED = ("/api", "/share", "/manifest.json", "/sw.js", "/pwa")
_URL_RE = re.compile(r"https://[^\s<>\"']+")

app = FastAPI(title="ContentPilot", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _undo_doubled_mount(request: Request, call_next):
    """If a proxy forwards /api/ingest as /api/api/ingest (mount point not stripped), undo it."""
    path = request.scope["path"]
    for prefix in _MOUNTED:
        if path.startswith(prefix + prefix):
            request.scope["path"] = path[len(prefix):]
            break
    return await call_next(request)


# --- /api ----------------------------------------------------------------------------------

class IngestBody(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=service.INGEST_MAX_URLS)
    intent: str | None = Field(default=None, max_length=500)


def require_token(x_ingest_token: str | None = Header(default=None)) -> None:
    expected = config.ingest_token()
    if not expected:
        raise HTTPException(503, "INGEST_TOKEN is not set on the server")
    if not x_ingest_token or not secrets.compare_digest(x_ingest_token.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong X-Ingest-Token")


@app.post("/api/ingest", dependencies=[Depends(require_token)])
def ingest(body: IngestBody) -> dict:
    with service.transaction() as conn:
        results = service.ingest_urls(conn, body.urls, body.intent)
    return {"counts": dict(Counter(r["status"] for r in results)), "results": results}


@app.get("/api/health")
def health() -> JSONResponse:
    from contentpilot import storage
    from contentpilot.copilot import spend
    try:
        with service.transaction() as conn:
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("sources", "post_state", "actions", "outputs", "prep_notes", "embeddings")}
            counts["open"] = conn.execute("SELECT COUNT(*) FROM post_state WHERE status = 'open'").fetchone()[0]
            body = {"ok": True, "db": counts, "spent_today_usd": round(spend.spent_today(conn), 4),
                    "daily_cap_usd": spend.get_daily_cap(conn), "chroma": storage.chroma_available(),
                    "ingest_token_set": config.ingest_token() is not None}
        return JSONResponse(body)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)


# --- installable web app ------------------------------------------------------------------------

@app.get("/manifest.json")
def manifest() -> JSONResponse:
    icons = [{"src": "/pwa/icon-192.png", "sizes": "192x192", "type": "image/png"},
             {"src": "/pwa/icon-512.png", "sizes": "512x512", "type": "image/png"},
             {"src": "/pwa/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]
    body = {
        "name": "ContentPilot",
        "short_name": "ContentPilot",
        "description": "Save posts to your ContentPilot Inbox.",
        "id": "/",
        "start_url": config.pwa_start_url(),
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#0f766e",
        "icons": icons,
        "share_target": {"action": "/share", "method": "GET",
                         "params": {"title": "title", "text": "text", "url": "url"}},
    }
    return JSONResponse(body, media_type="application/manifest+json")


_SW = """// ContentPilot service worker: makes the app installable. Network first, no caching of data.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return;   // let everything else go straight to the network
  event.respondWith(fetch(event.request).catch(() => new Response(
    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<body style="font:18px system-ui;padding:24px">ContentPilot is offline. ' +
    'Check that Tailscale is connected, then try again.</body>',
    {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
});
"""


@app.get("/sw.js")
def service_worker() -> Response:
    return Response(_SW, media_type="text/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/pwa/{name}")
def pwa_icon(name: str) -> FileResponse:
    if name not in ICONS:
        raise HTTPException(404)
    return FileResponse(WEB_DIR / name, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# --- share target page -----------------------------------------------------------------------------

def first_link(url: str | None, text: str | None) -> str:
    """The link to save: `url` if given, else the first https:// link in `text` (LinkedIn puts it there)."""
    url = (url or "").strip()
    if url.startswith(("https://", "http://")):
        return url
    match = _URL_RE.search(text or "")
    return match.group(0).rstrip(".,;:!?)]}»”'\"") if match else ""


_CSS = """
:root { --bg:#ffffff; --fg:#111827; --muted:#6b7280; --card:#f3f4f6; --accent:#0f766e; --accent-fg:#ffffff;
        --ok:#065f46; --ok-bg:#d1fae5; --warn:#92400e; --warn-bg:#fef3c7; --err:#991b1b; --err-bg:#fee2e2; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0b1120; --fg:#e5e7eb; --muted:#9ca3af; --card:#1f2937; --accent:#14b8a6; --accent-fg:#042f2e;
          --ok:#a7f3d0; --ok-bg:#064e3b; --warn:#fde68a; --warn-bg:#78350f; --err:#fecaca; --err-bg:#7f1d1d; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:18px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:560px; margin:0 auto; padding:20px 16px 40px; }
h1 { font-size:24px; margin:4px 0 16px; }
.card { background:var(--card); border-radius:14px; padding:14px 16px; margin:0 0 16px; overflow-wrap:anywhere; }
.muted { color:var(--muted); font-size:15px; }
label { display:block; font-weight:600; margin:0 0 8px; }
textarea, input[type=url] { width:100%; font:inherit; padding:14px; border-radius:12px; border:1px solid var(--muted);
        background:var(--bg); color:var(--fg); }
textarea { min-height:96px; resize:vertical; }
.btn { display:block; width:100%; min-height:56px; margin:16px 0 0; border:0; border-radius:14px; font:inherit;
       font-weight:700; text-align:center; line-height:56px; text-decoration:none; cursor:pointer;
       background:var(--accent); color:var(--accent-fg); }
.btn.secondary { background:var(--card); color:var(--fg); }
.btn[disabled] { opacity:.6; }
.status { border-radius:14px; padding:14px 16px; margin:0 0 16px; font-weight:600; }
.ok { background:var(--ok-bg); color:var(--ok); } .warn { background:var(--warn-bg); color:var(--warn); }
.err { background:var(--err-bg); color:var(--err); }
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f766e">
<link rel="manifest" href="/manifest.json"><link rel="icon" href="/pwa/icon-192.png">
<title>{html.escape(title)}</title><style>{_CSS}</style></head>
<body><main>{body}</main>
<script>if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js', {{scope: '/'}}).catch(() => {{}});</script>
</body></html>""")


@app.get("/share", response_class=HTMLResponse)
def share_form(url: str | None = None, text: str | None = None, title: str | None = None) -> HTMLResponse:
    link = first_link(url, text)
    inbox = html.escape(config.inbox_url())
    if not link:
        body = (f"<h1>ContentPilot</h1>"
                f"<div class='card'>Share a post to <b>ContentPilot</b> from LinkedIn or Chrome, "
                f"or paste a link here.</div>"
                f"<form method='post' action='/share' onsubmit='busy(this)'>"
                f"<label for='url'>Link</label><input id='url' name='url' type='url' required placeholder='https://'>"
                f"{_intent_field()}<button class='btn' type='submit'>Add</button></form>"
                f"<a class='btn secondary' href='{inbox}'>Open Inbox</a>"
                f"<p class='muted'>To get ContentPilot in your Share menu: Chrome menu ⋮ → <b>Install app</b> "
                f"(or <b>Add to Home screen</b>).</p>{_BUSY_JS}")
        return _page("ContentPilot", body)
    shown_title = (title or "").strip()
    body = (f"<h1>Add to ContentPilot</h1>"
            f"<div class='card'>{('<b>' + html.escape(shown_title) + '</b><br>') if shown_title else ''}"
            f"<span class='muted'>{html.escape(link)}</span></div>"
            f"<form method='post' action='/share' onsubmit='busy(this)'>"
            f"<input type='hidden' name='url' value='{html.escape(link, quote=True)}'>"
            f"{_intent_field()}<button class='btn' type='submit'>Add</button></form>"
            f"<a class='btn secondary' href='{inbox}'>Open Inbox</a>{_BUSY_JS}")
    return _page("Add to ContentPilot", body)


def _intent_field() -> str:
    return ("<label for='intent' style='margin-top:16px'>What do you want from it? <span class='muted'>(optional)</span>"
            "</label><textarea id='intent' name='intent' maxlength='500' "
            "placeholder='e.g. apply if it is a senior TPM role'></textarea>")


_BUSY_JS = ("<script>function busy(f){var b=f.querySelector('button');b.disabled=true;"
            "b.textContent='Adding… (a few seconds)';}</script>")


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return False
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").lower()
    return urlparse(origin).netloc.lower() == host


@app.post("/share", response_class=HTMLResponse)
def share_add(request: Request, url: str = Form(...), intent: str | None = Form(default=None),
              x_ingest_token: str | None = Header(default=None)) -> HTMLResponse:
    token_ok = bool(config.ingest_token() and x_ingest_token
                    and secrets.compare_digest(x_ingest_token.encode(), config.ingest_token().encode()))
    if not token_ok:
        if not _same_origin(request):
            raise HTTPException(403, "cross-site request refused")
        if config.share_requires_tailscale_identity() and not request.headers.get("tailscale-user-login"):
            raise HTTPException(403, "open this page through your tailnet (tailscale serve)")
    link = first_link(url, None)
    inbox = html.escape(config.inbox_url())
    if not link:
        return _page("ContentPilot", "<div class='status err'>That isn't a web link.</div>"
                                     f"<a class='btn secondary' href='/share'>Try again</a>")
    try:
        with service.transaction() as conn:
            res = service.ingest_urls(conn, [link], intent)[0]
    except Exception as exc:
        res = {"status": "failed", "detail": f"{type(exc).__name__}: {exc}"}

    status = res["status"]
    title = html.escape(res.get("title") or link)
    if status == "added":
        kind = (res.get("post_type") or "").replace("_", " ")
        note = ("" if res.get("triaged") else f"<p class='muted'>{html.escape(res.get('detail', ''))}</p>")
        where = ("It is in your Inbox." if res.get("post_status") == "open"
                 else f"Sorted as {html.escape(kind)} and filed automatically (see Done log).") if res.get("triaged") else ""
        msg = (f"<div class='status ok'>Added - open Inbox</div><div class='card'><b>{title}</b><br>"
               f"<span class='muted'>{html.escape(kind.capitalize()) if kind else 'Not sorted yet'} · {where}</span>"
               f"</div>{note}")
    elif status == "duplicate":
        msg = f"<div class='status ok'>Already in ContentPilot</div><div class='card'>{title}</div>"
    elif status == "blocked":
        msg = ("<div class='status warn'>LinkedIn didn't let ContentPilot read that post.</div>"
               "<p class='muted'>It may be private or behind a login. Open it on LinkedIn and use Save there; "
               "the laptop scan will pick it up.</p>")
    else:
        msg = (f"<div class='status err'>Couldn't add that link.</div>"
               f"<p class='muted'>{html.escape(res.get('detail') or '')}</p>")
    return _page("ContentPilot", f"<h1>ContentPilot</h1>{msg}<a class='btn' href='{inbox}'>Open Inbox</a>")
