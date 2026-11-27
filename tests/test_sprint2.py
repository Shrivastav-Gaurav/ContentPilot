"""Sprint 2 tests: display titles, blocked sign-up pages, and the service functions
the Inbox / Search / Done log / home pages use. Temp database, fake LLM and fake fetch.
"""
from datetime import datetime, timezone

import pytest

from test_copilot import DAY, JINA, NOW, conn, fake_llm_factory  # noqa: F401  (conn is a fixture)

UTC = timezone.utc


# --- 0a. display_title -------------------------------------------------------

@pytest.mark.parametrize("title, publisher, expected", [
    ("Databricks says evals are the bottleneck… | Mike Fishbein | 104 comments", "Mike Fishbein",
     "Databricks says evals are the bottleneck…"),
    ("Getting started: your first RL run | Sophia Yang | 12 comments", None, "Getting started: your first RL run"),
    ("RAG is not a plug and play feature | Manas Dasgupta posted on the topic | LinkedIn", None,
     "RAG is not a plug and play feature"),
    ("Senior Technical Program Manager, ShipTech | Amrita Chaudhri She/Her", "Amrita Chaudhri She/Her",
     "Senior Technical Program Manager, ShipTech"),
    ("Agents in production: a field guide | LinkedIn", None, "Agents in production: a field guide"),
    ("Hybrid retrieval beats embeddings | Stan Fedotov, PhD", None, "Hybrid retrieval beats embeddings"),
    # Sprint 2.1: the six real titles that still kept a "|" (publisher as stored).
    ("Your Body Language May Shape Who You Are | Amy Cuddy | TED", "TED", "Your Body Language May Shape Who You Are"),
    ("AI and GenAI for Managers: What Actually Works on Monday Morning | Sunil Pachunde PMP, CSM", None,
     "AI and GenAI for Managers: What Actually Works on Monday Morning"),
    # Not names: a series subtitle with a number, and a one-word location. These keep their "|".
    ("But what is a neural network? | Deep learning chapter 1", "3Blue1Brown",
     "But what is a neural network? | Deep learning chapter 1"),
    ("Backpropagation, intuitively | Deep Learning Chapter 3", "3Blue1Brown",
     "Backpropagation, intuitively | Deep Learning Chapter 3"),
    ("Gradient descent, how neural networks learn | Deep Learning Chapter 2", "3Blue1Brown",
     "Gradient descent, how neural networks learn | Deep Learning Chapter 2"),
    ("🚀 We’re Hiring | Staff Technical Program Manager – Walmart Global Tech | Bangalore!", "Mahima Sajwan",
     "🚀 We’re Hiring | Staff Technical Program Manager – Walmart Global Tech | Bangalore!"),
    # The name rule strips once: the role before the name stays.
    ("Hiring for our agent platform | Product Lead | Jane Doe", None, "Hiring for our agent platform | Product Lead"),
])
def test_display_title_strips_suffixes(title, publisher, expected):
    from contentpilot.copilot.parsing import display_title
    assert display_title(title, "the summary", publisher) == expected


@pytest.mark.parametrize("title", ["POST 11:", "POST 3", "Sign Up | LinkedIn", "Hi | LinkedIn", "", None])
def test_display_title_falls_back_to_summary(title):
    from contentpilot.copilot.parsing import display_title
    assert display_title(title, "Argues RAG is five engineering layers.") == "Argues RAG is five engineering layers."


def test_display_title_never_overwrites_source_title(conn):
    from contentpilot.copilot import service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    conn.execute("UPDATE sources SET title = 'POST 11:' WHERE id = 3")
    rows = {r["source_id"]: r for r in service.list_inbox(conn, now=NOW)}
    assert rows[3]["display_title"] == "s"  # the fake triage summary
    assert conn.execute("SELECT title FROM sources WHERE id = 3").fetchone()["title"] == "POST 11:"


# --- 0b. jina treats sign-up / login pages as blocked -------------------------

class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


_CHROME = "\n".join(f"[Link {i}](https://www.linkedin.com/x{i}) Join now Sign in Learn more" for i in range(80))


@pytest.mark.parametrize("title", ["Sign Up | LinkedIn", "LinkedIn Login", "Log In or Sign Up",
                                   "LinkedIn Login, Sign in | LinkedIn"])
def test_jina_signup_pages_are_blocked(monkeypatch, title):
    from contentpilot.fetchers import jina
    from contentpilot.fetchers.base import FetchBlockedError
    page = f"Title: {title}\n\nURL Source: https://www.linkedin.com/signup\n\nMarkdown Content:\n{_CHROME}"
    assert len(page) > 2000  # long enough to dodge the old short-page checks
    monkeypatch.setattr(jina.requests, "get", lambda *a, **k: _Resp(page))
    with pytest.raises(FetchBlockedError):
        jina.fetch("https://www.linkedin.com/posts/someone_activity-1")


def test_jina_real_post_still_fetches(monkeypatch):
    from contentpilot.fetchers import jina
    page = f"Title: How we run launch reviews | Priya Menon posted on the topic | LinkedIn\n\nMarkdown Content:\n{_CHROME}"
    monkeypatch.setattr(jina.requests, "get", lambda *a, **k: _Resp(page))
    assert jina.fetch("https://www.linkedin.com/posts/x").title.startswith("How we run launch reviews")


# --- due labels ----------------------------------------------------------------

def test_due_label_by_calendar_day():
    from contentpilot.copilot.rules import due_label
    now = int(datetime(2026, 11, 26, 15, 0, tzinfo=UTC).timestamp())  # Thursday
    at = lambda d, h=9: int(datetime(2026, 11, d, h, 0, tzinfo=UTC).timestamp())  # noqa: E731
    assert due_label(at(26, 8), now, UTC) == "due today"        # earlier today is still "today"
    assert due_label(at(24), now, UTC) == "overdue 2d"
    assert due_label(at(27), now, UTC) == "due tomorrow"
    assert due_label(at(28), now, UTC) == "due Sat"
    assert due_label(int(datetime(2026, 12, 4, tzinfo=UTC).timestamp()), now, UTC) == "due Dec 4"
    assert due_label(None, now, UTC) is None


# --- service functions for the pages ---------------------------------------------

def _fake_fetch(url, title="Shipping agents safely in large orgs", body="Hiring a TPM to own agent launches."):
    from contentpilot.fetchers.base import RawContent
    return RawContent(
        source_type="linkedin", external_id=url, canonical_url=url, title=title, author_name="Lena Ortiz",
        author_channel_id=None, author_url=None, published_at=None, duration_sec=None, language=None,
        text=JINA.format(title=title, n=99, published="2026-11-24T10:00:00.000Z", author="Lena Ortiz", body=body),
    )


def test_add_link_saves_intent_and_triages_only_that_post(conn):
    from contentpilot.copilot import service
    calls = []
    result = service.add_link(conn, "https://li/new", "apply if senior", fetch=_fake_fetch,
                              llm=fake_llm_factory(calls), now=NOW)
    conn.commit()
    assert result["new"] and result["post"]["post_type"] == "job" and result["post"]["status"] == "open"
    assert len(calls) == 1 and calls[0].count('<post id="') == 1      # the 6 fixture posts were not sent
    assert "<intent>apply if senior</intent>" in calls[0]
    row = conn.execute("SELECT intent, saved_at, published_at FROM sources WHERE id = ?", (result["source_id"],)).fetchone()
    assert row["intent"] == "apply if senior" and row["saved_at"] == NOW and row["published_at"] > 0

    again = service.add_link(conn, "https://li/new", "", fetch=_fake_fetch, llm=fake_llm_factory(calls), now=NOW)
    assert not again["new"] and len(calls) == 1                         # no second LLM call


def test_save_to_notes_appends_and_is_independent_of_postpilot(conn, tmp_path):
    from contentpilot.copilot import service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    notes = tmp_path / "notes.md"
    service.record_action(conn, 6, "send_postpilot", destination="PostPilot", now=NOW)
    service.save_to_notes(conn, 6, path=str(notes), now=NOW + 1)
    service.save_to_notes(conn, 6, path=str(notes), now=NOW + 2)
    text = notes.read_text(encoding="utf-8")
    assert text.count("## Strong opinion on agents") == 1   # the second save is a no-op (idempotent)
    assert "- Link: https://li/6" in text and "- Angle: a" in text
    actions = [d["action"] for d in service.list_done(conn) if d["source_id"] == 6]
    assert actions == ["save_notes", "send_postpilot"]


def test_done_log_filters_and_automatic_flag(conn):
    from contentpilot.copilot import service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    service.record_action(conn, 3, "file_to_prep", destination="Interview Prep", now=NOW)
    done = service.list_done(conn)
    auto = {d["source_id"] for d in done if d["automatic"]}
    assert auto == {2, 4}                                    # old job archived, noise dismissed
    assert [d["source_id"] for d in service.list_done(conn, action="file_to_prep")] == [3]
    assert set(service.done_action_types(conn)) == {"archive", "dismiss", "file_to_prep"}
    assert all(d["display_title"] for d in done)


def test_home_counts(conn):
    from contentpilot.copilot import service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    service.record_action(conn, 3, "read", now=NOW)
    conn.execute("UPDATE post_state SET due_at = ? WHERE source_id = 5", (NOW - 3 * DAY,))
    c = service.home_counts(conn, now=NOW, tz=UTC)
    assert c["open_by_type"] == {"job": 1, "learning": 0, "post_idea": 1, "tool": 1}
    assert c["open_total"] == 3 and c["overdue"] == 1
    assert c["done_this_week"] == 1                          # automatic archive/dismiss not counted
    service.move_back_to_inbox(conn, 3, now=NOW + 1)
    assert service.home_counts(conn, now=NOW + 1, tz=UTC)["done_this_week"] == 0


def test_search_results_have_display_title_and_source_types(conn):
    from contentpilot.copilot import search, service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    results = search.search(conn, "Anika Rao")
    assert results[0]["display_title"] == "RAG is no longer simple"
    assert search.source_types(conn) == ["linkedin"]


def test_search_quoted_text_is_an_exact_phrase(conn):
    from contentpilot.copilot import search, service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    # Post 3 body: "Hybrid retrieval and reranking beat plain embeddings."
    ids = lambda q: [r["id"] for r in search.search(conn, q)]  # noqa: E731
    assert ids("reranking retrieval") == [3]                  # all words, any order
    assert ids('"retrieval and reranking"') == [3]            # exact phrase
    assert ids('"reranking retrieval"') == []                 # phrase in the wrong order
    assert ids('"retrieval and" embeddings') == [3]           # phrase plus a plain word
    assert ids('"retrieval and" Ellison') == []               # ...all must match
    assert ids('"hybrid retrieval') == [3]                    # unpaired quote: plain words
    assert ids('""') == ids("")                               # empty phrase is no filter


# --- worker: triage after a saved scan --------------------------------------------

def test_worker_triages_after_saved_scan(conn, monkeypatch):
    from contentpilot import worker
    from contentpilot.storage import create_ingest_job
    job_id = create_ingest_job("saved_scan", "linkedin-saved-posts", [], cost_cap_usd=1.0)
    seen = []
    monkeypatch.setattr(worker, "_triage_new_sources", lambda llm=None: seen.append(job_id))
    worker._run_job(dict(conn.execute("SELECT * FROM ingest_jobs WHERE id = ?", (job_id,)).fetchone()))
    assert seen == [job_id]


def test_triage_new_sources_commits(conn):
    from contentpilot import worker
    stats = worker._triage_new_sources(llm=fake_llm_factory([]))
    assert stats["triaged"] == 6
    assert conn.execute("SELECT COUNT(*) FROM post_state").fetchone()[0] == 6
