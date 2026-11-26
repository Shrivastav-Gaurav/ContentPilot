"""Every OpenRouter call sends auth plus the app attribution headers. No network: requests.post is faked."""
import pytest

EXPECTED = {
    "Authorization": "Bearer test-key",
    "HTTP-Referer": "https://github.com/Shrivastav-Gaurav/ContentPilot",
    "X-Title": "ContentPilot",
}


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}}


@pytest.fixture()
def sent(monkeypatch):
    import requests
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers})
        return _Resp()
    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def test_helper(monkeypatch):
    from contentpilot import config
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert config.openrouter_headers() == EXPECTED


def test_triage_fit_and_prep_calls(sent):
    # Fit and prep notes go through triage.openrouter_llm.
    from contentpilot.copilot import triage
    triage.openrouter_llm("system", "user")
    assert sent[-1]["headers"] == EXPECTED


def test_extraction_call(sent):
    from contentpilot import extraction
    extraction._call_openrouter("some content")
    assert sent[-1]["headers"] == EXPECTED


def test_synthesizer_call(sent):
    from contentpilot.rag import synthesizer
    synthesizer._call_openrouter("question", [])
    assert sent[-1]["headers"] == EXPECTED


def test_card_chat_call(sent, monkeypatch):
    # Card chat logs its cost to outputs; conftest.py keeps that in a temp database.
    from contentpilot.rag import card_chat_agent
    monkeypatch.setattr(card_chat_agent, "get_card_chat_context", lambda cid: {
        "title": "t", "summary": "s", "key_learnings": [], "build_ideas": [], "job_signals": [],
        "content_snippet": "c", "author": "a", "url": "u"})
    monkeypatch.setattr(card_chat_agent, "save_chat_turn", lambda *a: None)
    monkeypatch.setattr(card_chat_agent, "_build_system_prompt", lambda ctx: "system")
    card_chat_agent.chat(1, "hello", history=[])
    assert sent[-1]["headers"] == EXPECTED
    assert sent[-1]["url"].startswith("https://openrouter.ai/")
