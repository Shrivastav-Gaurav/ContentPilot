"""Sprint 1 tests. Run: python -m pytest tests -q

Uses a temporary database and a fake LLM, so no API calls and no key needed.
"""
import json
import sqlite3

import pytest

DAY = 86_400
NOW = 1_795_600_000  # 2026-11-25

JINA = (
    "Title: {title}\n\nURL Source: https://www.linkedin.com/feed/update/urn:li:activity:{n}\n\n"
    "Published Time: {published}\n\nMarkdown Content:\n## {author}’s Post\n\n{body}"
)


def fake_llm_factory(calls):
    def fake_llm(system, user):
        calls.append(user)
        items = []
        for chunk in user.split('<post id="')[1:]:
            pid = int(chunk.split('"', 1)[0])
            low = chunk.lower()
            if "hiring" in low:
                kind, role = "job", "Senior TPM"
            elif "work with me" in low:
                kind, role = "noise", None
            elif "tool" in low:
                kind, role = "tool", None
            elif "opinion" in low:
                kind, role = "post_idea", None
            else:
                kind, role = "learning", None
            items.append({"id": pid, "post_type": kind, "summary": "s", "suggested_action": "a",
                          "topics": ["rag"], "role_title": role})
        return json.dumps({"items": items}), {"cost": 0.001}
    return fake_llm


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    # Point the db module straight at a temp file. Never touches the real database.
    from contentpilot import db
    test_db = str(tmp_path / "t.sqlite3")
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    c = db.get_connection()
    assert c.execute("PRAGMA database_list").fetchone()[2] == test_db, "refusing to run on a non-temp database"
    posts = [
        ("We are hiring a Senior TPM", "2026-11-20T10:00:00.000Z", "Priya Menon", "We are hiring a Senior TPM for AI platform."),
        ("Old hiring post", "2026-08-01T10:00:00.000Z", "Mark Ellison", "Hiring remote PMs."),
        ("RAG is no longer simple", "2026-10-12T10:00:00.000Z", "Anika Rao", "Hybrid retrieval and reranking beat plain embeddings."),
        ("Want to work with me?", "2026-09-16T10:00:00.000Z", "Dana Cole", "Want to work with me? Coaching offer."),
        ("New tool for evals", "2026-10-18T10:00:00.000Z", "Ravi Kumar", "A tool for agent evals."),
        ("Strong opinion on agents", "2026-10-08T10:00:00.000Z", "Stefan Volkov", "My opinion: eval gates matter."),
    ]
    for i, (title, pub, author, body) in enumerate(posts, start=1):
        c.execute(
            "INSERT INTO sources (source_type, canonical_url, raw_text, fetched_at, saved_at) VALUES ('linkedin', ?, ?, ?, ?)",
            (f"https://li/{i}", JINA.format(title=title, n=i, published=pub, author=author, body=body), NOW - DAY, NOW - DAY),
        )
    c.commit()
    yield c
    c.close()


def test_facts_without_llm(conn):
    from contentpilot.copilot import service
    counts = service.backfill_facts(conn)
    assert counts == {"published_at": 6, "title": 6, "publisher": 6}
    row = conn.execute("SELECT s.published_at, a.name FROM sources s JOIN authors a ON a.id = s.author_id WHERE s.id = 3").fetchone()
    assert row["name"] == "Anika Rao" and row["published_at"] > 0


def test_triage_rules_and_cache(conn):
    from contentpilot.copilot import service
    service.backfill_facts(conn)
    calls = []
    stats = service.run_triage(conn, llm=fake_llm_factory(calls), now=NOW, batch_size=10)
    assert stats["triaged"] == 6 and stats["llm_calls"] == 1 and len(calls) == 1

    st = {r["source_id"]: dict(r) for r in conn.execute("SELECT * FROM post_state")}
    assert st[1]["post_type"] == "job" and st[1]["status"] == "open"
    assert st[2]["status"] == "archived"          # job published > 90 days ago
    assert st[4]["status"] == "dismissed"         # noise
    assert st[3]["due_at"] == NOW - DAY + 7 * DAY  # learning: saved + 7 days

    # Re-triage after state is wiped: served from cache, zero LLM calls.
    conn.execute("DELETE FROM post_state")
    calls.clear()
    stats = service.run_triage(conn, llm=fake_llm_factory(calls), now=NOW)
    assert stats["cached"] == 6 and stats["llm_calls"] == 0 and calls == []


def test_daily_cap_stops_cleanly(conn):
    from contentpilot.copilot import service
    calls = []
    stats = service.run_triage(conn, llm=fake_llm_factory(calls), now=NOW, daily_cap_usd=0.0)
    assert stats["stopped_by_cap"] and calls == []


def test_actions_done_log_and_move_back(conn):
    from contentpilot.copilot import service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    inbox_before = {r["source_id"] for r in service.list_inbox(conn, now=NOW)}
    assert inbox_before == {1, 3, 5, 6}

    service.record_action(conn, 6, "send_postpilot", destination="PostPilot ideas", now=NOW)
    service.record_action(conn, 6, "save_notes", destination="notes file", now=NOW + 1)  # both allowed
    service.record_action(conn, 5, "park", now=NOW - 5)
    assert {r["source_id"] for r in service.list_inbox(conn, now=NOW)} == {1, 3}

    done = service.list_done(conn)
    assert [d["action"] for d in done[:2]] == ["save_notes", "send_postpilot"]

    service.move_back_to_inbox(conn, 5, now=NOW + 10)
    assert 5 in {r["source_id"] for r in service.list_inbox(conn, now=NOW + 10)}
    assert conn.execute("SELECT COUNT(*) FROM actions WHERE source_id = 5 AND undone_at IS NOT NULL").fetchone()[0] >= 1


def test_job_status_is_users_choice(conn):
    from contentpilot.copilot import service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)

    service.mark_apply_pack_copied(conn, 1, now=NOW)
    jt = conn.execute("SELECT user_status FROM job_tracking WHERE source_id = 1").fetchone()
    assert jt["user_status"] == "not_started"
    assert conn.execute("SELECT status FROM post_state WHERE source_id = 1").fetchone()["status"] == "open"

    service.set_job_status(conn, 1, "deferred", note="waiting on referral", now=NOW)
    assert 1 not in {r["source_id"] for r in service.list_inbox(conn, now=NOW + DAY)}
    assert 1 in {r["source_id"] for r in service.list_inbox(conn, now=NOW + 8 * DAY)}

    service.set_job_status(conn, 1, "applied", now=NOW + 9 * DAY)
    assert conn.execute("SELECT status FROM post_state WHERE source_id = 1").fetchone()["status"] == "done"
    with pytest.raises(ValueError):
        service.set_job_status(conn, 1, "submitted")


def test_search_everything_including_archived(conn):
    from contentpilot.copilot import search, service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    search.rebuild_index(conn)

    by_publisher = search.search(conn, "Mark Ellison")
    assert [r["id"] for r in by_publisher] == [2] and by_publisher[0]["status"] == "archived"
    assert [r["id"] for r in search.search(conn, "rerank")] == [3]          # prefix match on body
    assert {r["id"] for r in search.search(conn, status="dismissed")} == {4}
    assert search.search(conn, 'weird "quotes" AND (') == [] or True        # never raises

    service.set_job_status(conn, 1, "in_progress", note="referral from Anika", now=NOW)
    assert 1 in {r["id"] for r in search.search(conn, "referral")}          # your notes are searchable
