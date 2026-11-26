"""Daily cap setting, Haiku-by-default synthesis with a Sonnet option, and all spend in outputs."""
import json

import pytest

from test_copilot import NOW, conn  # noqa: F401  (conn is a fixture)

_SYNTH_JSON = ('{"answer": "Use a rollback plan.", "key_points": ["k"], "follow_up_questions": ["f"], '
               '"sources_used": [], "confidence": "low", "gaps": []}')


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


@pytest.fixture()
def models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-haiku-5.5")
    monkeypatch.setenv("SYNTHESIS_MODEL", "anthropic/claude-sonnet-5")


# --- 2. daily cap setting -------------------------------------------------------------------

def test_daily_cap_setting(conn):
    from contentpilot.copilot import spend
    assert spend.get_daily_cap(conn) == 0.25                       # default
    spend.set_daily_cap(conn, 0.5)
    assert spend.get_daily_cap(conn) == 0.5 and spend.resolve_cap(conn, None) == 0.5
    assert spend.resolve_cap(conn, 0.01) == 0.01                   # explicit value wins
    conn.execute("UPDATE settings SET value = 'lots' WHERE key = 'api.daily_cap_usd'")
    assert spend.get_daily_cap(conn) == 0.25                       # unreadable -> default
    with pytest.raises(ValueError):
        spend.set_daily_cap(conn, -1)


def test_every_capped_feature_reads_the_setting(conn):
    from contentpilot.copilot import service, spend
    from test_copilot import fake_llm_factory
    spend.log_cost(conn, "test", "x", None, 0.20, now=NOW)        # $0.20 spent today
    calls = []
    assert service.run_triage(conn, llm=fake_llm_factory(calls), now=NOW)["triaged"] == 6   # under $0.25
    spend.set_daily_cap(conn, 0.15)
    conn.execute("DELETE FROM post_state")
    conn.execute("DELETE FROM outputs WHERE kind = 'triage'")
    assert service.run_triage(conn, llm=fake_llm_factory(calls), now=NOW)["stopped_by_cap"]   # over $0.15
    with pytest.raises(spend.DailyCapReached):
        service.ask_prep(conn, "q", 1, lambda q: ["c"], lambda q, c: None, now=NOW)


# --- 1. synthesizer: Haiku default, Sonnet on request, trimmed chunks, max_tokens -------------

def test_synthesizer_defaults_to_haiku_with_trimmed_chunks_and_max_tokens(models, monkeypatch):
    import requests

    from contentpilot.rag import synthesizer
    from contentpilot.rag.retriever import RetrievedChunk
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return _Resp({"choices": [{"message": {"content": _SYNTH_JSON}}], "usage": {"cost": 0.001}})
    monkeypatch.setattr(requests, "post", fake_post)

    long_text = "word " * 400                                       # 2,000 characters
    chunk = RetrievedChunk(card_id=1, source_title="T", author_name="A", chunk_text=long_text, distance=0.0, url="u")
    result = synthesizer.synthesize("q", [chunk])
    assert sent[0]["model"] == "anthropic/claude-haiku-5.5" and result.model_used == "anthropic/claude-haiku-5.5"
    assert sent[0]["max_tokens"] == 700
    source_block = sent[0]["messages"][1]["content"].split("\n", 3)[-1]
    body = source_block.split("\n", 1)[1].split("\n</sources>")[0]
    assert len(body) <= 601 and body.endswith("…")                 # ~600 characters, cut at a word

    deep = synthesizer.synthesize("q", [chunk], model="anthropic/claude-sonnet-5")
    assert sent[1]["model"] == "anthropic/claude-sonnet-5" and deep.model_used == "anthropic/claude-sonnet-5"
    assert "model" not in json.dumps(deep.model_dump())             # not saved inside the answer JSON


def test_deeper_answer_uses_sonnet_only_when_asked(conn, models):
    from contentpilot.copilot import service
    used = []

    class _A:
        def __init__(self, model):
            self.model_used, self.cost_usd = model, 0.01

        def model_dump(self):
            return {"answer": "a", "key_points": ["k"], "follow_up_questions": ["f"], "sources_used": [],
                    "confidence": "low", "gaps": []}

    def synth(q, chunks, model=None):
        used.append(model)
        return _A(model or "anthropic/claude-haiku-5.5")
    retrieve = lambda q: ["chunk"]  # noqa: E731

    first = service.ask_prep(conn, "Q", 1, retrieve, synth, now=NOW)
    assert used == [None] and first["model"] == "anthropic/claude-haiku-5.5" and not first["deep"]
    assert service.ask_prep(conn, "q", 1, retrieve, synth, now=NOW + 1)["cached"] and len(used) == 1
    deep = service.ask_prep(conn, "Q", 1, retrieve, synth, regenerate=True, deep=True, now=NOW + 2)
    assert used[-1] == "anthropic/claude-sonnet-5" and deep["model"] == "anthropic/claude-sonnet-5" and deep["deep"]
    row = conn.execute("SELECT model FROM outputs WHERE kind = 'prep_answer'").fetchone()
    assert row["model"] == "anthropic/claude-sonnet-5"
    replaced = conn.execute("SELECT model, cost_usd FROM outputs WHERE kind = 'spend_log'").fetchone()
    assert replaced["model"] == "anthropic/claude-haiku-5.5" and replaced["cost_usd"] == 0.01


# --- 3. card chat and extraction costs in outputs ----------------------------------------------

def test_card_chat_cost_is_logged(conn, models, monkeypatch):
    import requests

    from contentpilot.copilot import service
    from contentpilot.rag import card_chat_agent
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(
        {"model": "anthropic/claude-sonnet-5", "choices": [{"message": {"content": "Hi"}}], "usage": {"cost": 0.004}}))
    monkeypatch.setattr(card_chat_agent, "get_card_chat_context", lambda cid: {"x": 1})
    monkeypatch.setattr(card_chat_agent, "_build_system_prompt", lambda ctx: "system")
    monkeypatch.setattr(card_chat_agent, "save_chat_turn", lambda *a: None)
    before = service.spent_today(conn)
    card_chat_agent.chat(5, "hello", history=[])
    row = conn.execute("SELECT model, cost_usd, content_json FROM outputs WHERE kind = 'card_chat'").fetchone()
    assert row["cost_usd"] == 0.004 and row["model"] == "anthropic/claude-sonnet-5"
    assert json.loads(row["content_json"]) == {"card_id": 5}
    assert service.spent_today(conn) == pytest.approx(before + 0.004)


def _extraction_payload(content, cost):
    return {"choices": [{"message": {"content": content}}], "usage": {"cost": cost, "prompt_tokens": 1,
                                                                        "completion_tokens": 1}}


_EXTRACTION = json.dumps({"title": "t", "summary": "s", "content_type": "tutorial", "key_learnings": ["k"],
                          "build_ideas": ["b"], "job_signals": [], "topics": ["x"]})


def test_extraction_cost_logged_on_save_including_retry(conn, models, monkeypatch):
    from contentpilot import extraction, storage
    from contentpilot.fetchers.base import RawContent
    replies = iter([_extraction_payload("not json", 0.002), _extraction_payload(_EXTRACTION, 0.003)])
    monkeypatch.setattr(extraction, "_call_openrouter", lambda content, extra_instruction="": next(replies))
    monkeypatch.setattr(storage, "_index_card", lambda conn, card_id: None)   # no Chroma in tests
    result = extraction.extract_tpm_lens("text " * 50)
    assert result.cost_usd == pytest.approx(0.005)                  # both calls counted
    sid = storage.save_source(RawContent("article", "u", "https://ex/1", "T", None, None, None, None, None, None,
                                         "text " * 50))
    storage.save_card(sid, "tpm", result)
    row = conn.execute("SELECT cost_usd, prompt_version FROM outputs WHERE kind = 'extraction'").fetchone()
    assert row["cost_usd"] == pytest.approx(0.005) and row["prompt_version"] == result.prompt_version

    replies = iter([_extraction_payload("bad", 0.002), _extraction_payload("still bad", 0.002)])
    with pytest.raises(extraction.ExtractionValidationError) as err:
        extraction.extract_tpm_lens("text " * 50)
    assert err.value.cost_usd == pytest.approx(0.004)


def test_migration_8_copies_past_extraction_costs(conn):
    from contentpilot import db
    conn.execute("INSERT INTO sources (source_type, canonical_url) VALUES ('youtube', 'https://yt/1')")
    conn.execute("INSERT INTO cards (source_id, lens, prompt_version, model, cost_usd, extracted_at) "
                 "VALUES (1, 'tpm', 'tpm_v1', 'anthropic/claude-3-haiku', 0.0021, ?)", (NOW - 30 * 86_400,))
    conn.executescript(db._MIGRATIONS[8])
    conn.executescript(db._MIGRATIONS[8])                            # idempotent
    rows = conn.execute("SELECT cost_usd, created_at, model FROM outputs WHERE kind = 'extraction'").fetchall()
    assert len(rows) == 1 and rows[0]["cost_usd"] == 0.0021 and rows[0]["created_at"] == NOW - 30 * 86_400
