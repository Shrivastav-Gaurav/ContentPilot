"""FastAPI service: /api/ingest, /api/health, the Android share target, manifest and service worker.
No network: the fetcher and the OpenRouter HTTP call are faked. conftest.py gives each test a temp DB."""
import json

import pytest
from fastapi.testclient import TestClient

from test_copilot import JINA

TOKEN = "t" * 48
HOST = "contentpilot.example.ts.net"


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


@pytest.fixture()
def calls(monkeypatch):
    """Fake fetch + fake triage LLM (via requests.post). Returns counters."""
    import requests

    from contentpilot.fetchers import router
    from contentpilot.fetchers.base import FetchBlockedError, FetchUnavailableError, RawContent
    seen = {"fetch": [], "llm": 0}

    def fake_fetch(url):
        seen["fetch"].append(url)
        if "blocked" in url:
            raise FetchBlockedError("login wall")
        if "gone" in url:
            raise FetchUnavailableError("page not found")
        n = len(seen["fetch"])
        title = "We are hiring a Senior TPM" if "job" in url else f"How we run launch reviews {n}"
        text = JINA.format(title=title, n=n, published="2026-11-20T10:00:00.000Z", author="Priya Menon",
                           body=title + ". Details inside.")
        return RawContent("linkedin", url, url.split("?")[0], title, "Priya Menon", None, None, None, None, None, text)

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["llm"] += 1
        user = json["messages"][1]["content"]
        items = []
        for chunk in user.split('<post id="')[1:]:
            pid = int(chunk.split('"', 1)[0])
            job = "hiring" in chunk.lower()
            items.append({"id": pid, "post_type": "job" if job else "learning", "summary": "s",
                          "suggested_action": "a", "topics": ["t"], "role_title": "Senior TPM" if job else None})
        return _Resp({"choices": [{"message": {"content": __import__("json").dumps({"items": items})}}],
                      "usage": {"cost": 0.0005}})

    monkeypatch.setattr(router, "fetch", fake_fetch)
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)
    return seen


@pytest.fixture()
def client():
    from contentpilot.api import app
    return TestClient(app, base_url=f"https://{HOST}")


# --- /api/ingest -------------------------------------------------------------------------------------

def test_ingest_rejects_missing_or_wrong_token(client, calls, monkeypatch):
    body = {"urls": ["https://www.linkedin.com/posts/a-1"]}
    assert client.post("/api/ingest", json=body).status_code == 401
    assert client.post("/api/ingest", json=body, headers={"X-Ingest-Token": "wrong"}).status_code == 401
    monkeypatch.delenv("INGEST_TOKEN")
    assert client.post("/api/ingest", json=body, headers={"X-Ingest-Token": TOKEN}).status_code == 503
    assert calls["fetch"] == [] and calls["llm"] == 0


def test_ingest_adds_dedupes_and_reports_each_url(client, calls):
    from contentpilot.copilot import service
    urls = ["https://www.linkedin.com/posts/job-1?utm_source=share",
            "https://www.linkedin.com/posts/learn-2",
            "https://www.linkedin.com/posts/job-1?utm_source=other",    # same post: duplicate before fetching
            "https://www.linkedin.com/posts/blocked-3",
            "https://example.com/gone",
            "not a link"]
    r = client.post("/api/ingest", json={"urls": urls, "intent": "apply if senior"},
                    headers={"X-Ingest-Token": TOKEN})
    assert r.status_code == 200
    out = r.json()
    assert [x["status"] for x in out["results"]] == ["added", "added", "duplicate", "blocked", "failed", "failed"]
    assert out["counts"] == {"added": 2, "duplicate": 1, "blocked": 1, "failed": 2}
    assert calls["llm"] == 1                                        # both new posts triaged in one call
    job = out["results"][0]
    assert job["post_type"] == "job" and job["post_status"] == "open" and job["triaged"]

    with service.transaction() as conn:
        row = conn.execute("SELECT intent, saved_at FROM sources WHERE id = ?", (job["source_id"],)).fetchone()
        assert row["intent"] == "apply if senior" and row["saved_at"]
        assert len(service.list_inbox(conn)) == 2

    fetched = len(calls["fetch"])
    again = client.post("/api/ingest", json={"urls": urls[:2]}, headers={"X-Ingest-Token": TOKEN}).json()
    assert [x["status"] for x in again["results"]] == ["duplicate", "duplicate"]
    assert len(calls["fetch"]) == fetched and calls["llm"] == 1    # nothing fetched or triaged again


def test_ingest_validates_body(client, calls):
    h = {"X-Ingest-Token": TOKEN}
    assert client.post("/api/ingest", json={"urls": []}, headers=h).status_code == 422
    assert client.post("/api/ingest", json={"urls": ["https://x"] * 51}, headers=h).status_code == 422


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert set(r.json()["db"]) >= {"sources", "post_state", "outputs", "prep_notes", "embeddings", "open"}


def test_doubled_mount_path_still_routes(client):
    assert client.get("/api/api/health").status_code == 200        # proxy that doesn't strip /api


# --- installable app --------------------------------------------------------------------------------

def test_manifest_and_service_worker(client):
    m = client.get("/manifest.json")
    assert m.headers["content-type"].startswith("application/manifest+json")
    body = m.json()
    assert body["name"] == "ContentPilot" and body["display"] == "standalone"
    assert body["share_target"] == {"action": "/share", "method": "GET",
                                    "params": {"title": "title", "text": "text", "url": "url"}}
    assert {i["sizes"] for i in body["icons"]} == {"192x192", "512x512"}
    for icon in body["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert client.get("/pwa/../../.env").status_code == 404
    sw = client.get("/sw.js")
    assert sw.headers["content-type"].startswith("text/javascript") and "fetch" in sw.text


# --- share target -----------------------------------------------------------------------------------

def test_first_link():
    from contentpilot.api import first_link
    text = "Check out this post by Priya: https://www.linkedin.com/posts/priya_launch-activity-123-abc). Great!"
    assert first_link("", text) == "https://www.linkedin.com/posts/priya_launch-activity-123-abc"
    assert first_link("https://a.example/x", text) == "https://a.example/x"
    assert first_link(None, "no link here") == ""


def test_share_page_pulls_link_from_text(client):
    r = client.get("/share", params={"text": "Saw this https://www.linkedin.com/posts/x-1 on LinkedIn",
                                     "title": "<b>Launch reviews</b>"})
    assert r.status_code == 200
    assert "https://www.linkedin.com/posts/x-1" in r.text and "What do you want from it?" in r.text
    assert "&lt;b&gt;Launch reviews&lt;/b&gt;" in r.text              # escaped
    assert 'rel="manifest"' in r.text and "serviceWorker" in r.text
    landing = client.get("/share")
    assert "Install app" in landing.text and "name='url'" in landing.text


def test_share_add_requires_same_origin_and_tailnet_user(client, calls):
    form = {"url": "https://www.linkedin.com/posts/learn-9", "intent": ""}
    origin = {"Origin": f"https://{HOST}"}
    assert client.post("/share", data=form).status_code == 403                                  # no Origin
    assert client.post("/share", data=form, headers={"Origin": "https://evil.example",
                                                     "Tailscale-User-Login": "me@x"}).status_code == 403
    assert client.post("/share", data=form, headers=origin).status_code == 403                  # not via tailnet
    assert calls["fetch"] == []

    r = client.post("/share", data=form, headers={**origin, "Tailscale-User-Login": "me@example.com"})
    assert r.status_code == 200 and "Added - open Inbox" in r.text and "href='/Inbox'" in r.text
    r = client.post("/share", data=form, headers={**origin, "Tailscale-User-Login": "me@example.com"})
    assert "Already in ContentPilot" in r.text

    blocked = client.post("/share", data={"url": "https://www.linkedin.com/posts/blocked-1"},
                          headers={"X-Ingest-Token": TOKEN})                                    # token also works
    assert "didn't let ContentPilot read" in blocked.text
