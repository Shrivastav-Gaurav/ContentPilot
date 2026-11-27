"""Sprint 5 tests: daily email, Sunday summary, keep-or-dismiss, notes hand-off, migration.

Temp database (see conftest), fake mail sender, no network, no LLM.
"""
import sqlite3
from datetime import datetime

import pytest

from contentpilot.copilot import digest, fit, search, service
from contentpilot.copilot.rules import DAY

HASH = "resumehash"
ENV = {"GMAIL_USER": "me@example.com", "GMAIL_APP_PASSWORD": "pw", "DIGEST_TO": "me@example.com",
       "APP_BASE_URL": "https://app.example.ts.net/"}


def ist(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=digest.TZ).timestamp())


# Fri 27 Nov 2026, 07:30 IST. 28 Nov is a Saturday, 29 Nov a Sunday.
FRI = ist(2026, 11, 27, 7, 30)
SUN_EVENING = ist(2026, 11, 29, 18, 5)


@pytest.fixture()
def conn():
    from contentpilot import db
    c = db.get_connection()
    yield c
    c.close()


class Sender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, settings, d):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append((settings["DIGEST_TO"], d))


def add_post(conn, sid, post_type="learning", due=None, status="open", published=None, title=None,
             fit_score=None, user_status=None, reminders=0, saved=None, publisher="Acme Co"):
    conn.execute("INSERT OR IGNORE INTO authors (id, name) VALUES (?, ?)", (sid, publisher))
    conn.execute("INSERT INTO sources (id, source_type, canonical_url, title, author_id, published_at, saved_at, "
                 "fetched_at) VALUES (?, 'linkedin', ?, ?, ?, ?, ?, ?)",
                 (sid, f"https://li/{sid}", title or f"Post {sid} on a longer topic", sid, published, saved or FRI - 3 * DAY, FRI))
    conn.execute("INSERT INTO post_state (source_id, post_type, status, summary, due_at, reminders_sent, updated_at) "
                 "VALUES (?, ?, ?, 's', ?, ?, ?)", (sid, post_type, status, due, reminders, FRI))
    if post_type == "job":
        conn.execute("INSERT INTO job_tracking (source_id, user_status, updated_at) VALUES (?, ?, ?)",
                     (sid, user_status or "not_started", FRI))
    if fit_score is not None:
        service._store_output(conn, fit.KIND, fit.cache_key(sid, HASH), fit.PROMPT_VERSION,
                              {"fit_score": fit_score, "reasons": ["r"], "employer": f"Co{sid}", "concern": "c"},
                              None, None, FRI)
    conn.commit()


# --- migration ---------------------------------------------------------------------

def test_migration_adds_digest_log_and_keeps_existing_data(tmp_path, monkeypatch):
    from contentpilot import db
    full = dict(db._MIGRATIONS)
    monkeypatch.setattr(db, "_MIGRATIONS", {k: v for k, v in full.items() if k <= 8})
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "old.sqlite3"))
    c = db.get_connection()
    add_post(c, 1, "learning", due=FRI - DAY, reminders=1)
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'digest_log'").fetchone() is None
    c.close()

    monkeypatch.setattr(db, "_MIGRATIONS", full)
    c = db.get_connection()          # upgrade in place
    c2 = db.get_connection()         # and a second run is a no-op
    try:
        assert c.execute("SELECT 1 FROM schema_migrations WHERE version = 9").fetchone()
        row = c.execute("SELECT status, reminders_sent FROM post_state WHERE source_id = 1").fetchone()
        assert (row["status"], row["reminders_sent"]) == ("open", 1)
        assert c.execute("SELECT COUNT(*) FROM digest_log").fetchone()[0] == 0
    finally:
        c.close()
        c2.close()


# --- once-per-day guard, time gate ---------------------------------------------------

def test_daily_sends_once_per_day_even_across_passes(conn):
    add_post(conn, 1, "learning", due=FRI - DAY)
    s = Sender()
    first = digest.deliver(conn, "daily", FRI, sender=s, env=ENV)
    again = digest.deliver(conn, "daily", FRI + 600, sender=s, env=ENV)         # a later pass, same day
    restart = digest.deliver(conn, "daily", FRI + 3600, sender=s, env=ENV)      # "restarted worker"
    assert first["status"] == "sent" and again["status"] == "already_done" and restart["status"] == "already_done"
    assert len(s.sent) == 1
    nxt = digest.deliver(conn, "daily", FRI + DAY, sender=s, env=ENV)
    assert nxt["status"] == "sent" and len(s.sent) == 2


def test_two_workers_cannot_both_claim(conn):
    from contentpilot import db
    other = db.get_connection()
    try:
        assert digest.claim(conn, "daily", "2026-11-27", FRI) is True
        assert digest.claim(other, "daily", "2026-11-27", FRI) is False
    finally:
        other.close()


def test_not_sent_before_0730_ist(conn):
    add_post(conn, 1, "learning", due=FRI - DAY)
    s = Sender()
    assert digest.deliver(conn, "daily", FRI - 60, sender=s, env=ENV)["status"] == "not_due"   # 07:29
    assert digest.deliver(conn, "daily", FRI, sender=s, env=ENV)["status"] == "sent"            # 07:30
    assert len(s.sent) == 1


def test_failed_send_is_retried_after_30_minutes_not_before(conn):
    add_post(conn, 1, "learning", due=FRI - DAY)
    bad = Sender(fail=True)
    assert digest.deliver(conn, "daily", FRI, sender=bad, env=ENV)["status"] == "failed"
    assert conn.execute("SELECT reminders_sent FROM post_state WHERE source_id = 1").fetchone()[0] == 0
    assert digest.deliver(conn, "daily", FRI + 600, sender=Sender(), env=ENV)["status"] == "already_done"
    good = Sender()
    assert digest.deliver(conn, "daily", FRI + 1900, sender=good, env=ENV)["status"] == "sent"
    assert len(good.sent) == 1


# --- empty day / missing settings -----------------------------------------------------

def test_empty_day_sends_nothing_and_is_not_retried(conn):
    s = Sender()
    assert digest.deliver(conn, "daily", FRI, sender=s, env=ENV)["status"] == "nothing_to_send"
    add_post(conn, 1, "learning", due=FRI - DAY)        # becomes due later the same day
    assert digest.deliver(conn, "daily", FRI + 3600, sender=s, env=ENV)["status"] == "already_done"
    assert s.sent == []
    assert conn.execute("SELECT status FROM digest_log").fetchone()[0] == "skipped"


def test_future_due_post_alone_sends_nothing(conn):
    add_post(conn, 1, "learning", due=FRI + 2 * DAY)
    s = Sender()
    assert digest.deliver(conn, "daily", FRI, sender=s, env=ENV)["status"] == "nothing_to_send"


def test_missing_gmail_settings_skips_without_crashing(conn, capsys):
    add_post(conn, 1, "learning", due=FRI - DAY)
    s = Sender()
    env = {k: v for k, v in ENV.items() if k != "GMAIL_APP_PASSWORD"}
    res = digest.deliver(conn, "daily", FRI, sender=s, env=env)
    assert res["status"] == "not_configured" and res["missing"] == ["GMAIL_APP_PASSWORD"]
    assert s.sent == [] and conn.execute("SELECT COUNT(*) FROM digest_log").fetchone()[0] == 0
    # settings added later the same day: it still goes out
    assert digest.deliver(conn, "daily", FRI + 60, sender=s, env=ENV)["status"] == "sent"


def test_run_scheduled_logs_missing_settings_once_a_day(conn, capsys, monkeypatch):
    for k in digest.REQUIRED_ENV:
        monkeypatch.delenv(k, raising=False)
    digest._warned.clear()
    digest.run_scheduled(conn, FRI)
    digest.run_scheduled(conn, FRI + 300)
    lines = [l for l in capsys.readouterr().out.splitlines() if "digest not sent" in l]
    assert len(lines) == 1 and "GMAIL_USER" in lines[0] and "pw" not in lines[0]


# --- content: due list, cap, jobs, decide ------------------------------------------------

def test_due_section_cap_order_grouping_and_links(conn):
    for i in range(1, 15):   # 14 due posts, 14..1 days overdue
        add_post(conn, i, "learning" if i % 2 else "tool", due=FRI - (15 - i) * DAY)
    add_post(conn, 20, "learning", due=FRI + 3 * DAY)    # not due
    add_post(conn, 21, "noise", due=FRI - DAY)           # never listed
    add_post(conn, 22, "learning", due=FRI - DAY, status="done")
    d = digest.build_daily(conn, FRI, "https://app.example.ts.net/")
    assert d.counts["due"] == 10 and len(d.reminded_ids) == 10
    assert d.reminded_ids == list(range(1, 11))                       # most overdue first, cut at 10
    assert d.text.index("Learning (5)") < d.text.index("Tool (5)")    # grouped by type
    assert "https://app.example.ts.net/Inbox?post=1" in d.text and "//Inbox" not in d.text
    assert "Post 20" not in d.text and "Post 21" not in d.text and "Post 22" not in d.text
    assert "overdue 14d" in d.text and "Acme Co" in d.text


def test_top_jobs_use_60_day_rule_status_and_existing_scores(conn):
    add_post(conn, 1, "job", due=FRI + DAY, published=FRI - 5 * DAY, fit_score=70)
    add_post(conn, 2, "job", due=FRI + DAY, published=FRI - 10 * DAY, fit_score=90)
    add_post(conn, 3, "job", due=FRI + DAY, published=FRI - 61 * DAY, fit_score=99)               # too old
    add_post(conn, 4, "job", due=FRI + DAY, published=FRI - 2 * DAY, fit_score=95, user_status="applied")
    add_post(conn, 5, "job", due=FRI + DAY, published=FRI - 2 * DAY, fit_score=94, user_status="deferred")
    add_post(conn, 6, "job", due=FRI + DAY, published=FRI - 2 * DAY, fit_score=93, user_status="not_a_fit")
    add_post(conn, 7, "job", due=FRI + DAY, published=FRI - 3 * DAY, fit_score=80)
    add_post(conn, 8, "job", due=FRI + DAY, published=FRI - 3 * DAY, fit_score=60)
    add_post(conn, 9, "job", due=FRI + DAY, published=FRI - 3 * DAY)                              # never scored
    jobs = digest.top_jobs(conn, HASH, FRI)
    assert [j["source_id"] for j in jobs] == [2, 7, 1]
    assert digest.top_jobs(conn, None, FRI) == []                      # no resume: no scores, no calls
    d = digest.build_daily(conn, FRI, "https://app.example.ts.net")
    assert d is None or "Post 3" not in d.text
    d = digest.build_daily(conn, FRI, "https://app.example.ts.net", HASH)
    assert d.counts["jobs"] == 3 and "fit 90" in d.text and "Post 3" not in d.text and "Post 4" not in d.text
    assert d.reminded_ids == []                                         # the jobs list is not "reminded"


def test_no_model_is_called_building_the_digest(conn, monkeypatch):
    add_post(conn, 1, "job", due=FRI - DAY, published=FRI - DAY, fit_score=80)
    from contentpilot.copilot import triage
    monkeypatch.setattr(triage, "openrouter_llm", lambda *a, **k: pytest.fail("LLM called"))
    digest.build_daily(conn, FRI, "https://x", HASH)
    digest.build_weekly(conn, FRI, "https://x", HASH)
    assert conn.execute("SELECT COUNT(*) FROM outputs WHERE kind != 'fit'").fetchone()[0] == 0


# --- 2 ignored reminders -> Decide; Keep / Dismiss ----------------------------------------------

def test_two_ignored_reminders_move_a_post_to_decide(conn):
    add_post(conn, 1, "learning", due=FRI - DAY, title="Ignored one post about RAG")
    s = Sender()
    for n in (1, 2):
        res = digest.deliver(conn, "daily", FRI + (n - 1) * DAY, sender=s, env=ENV)
        assert res["status"] == "sent"
        assert conn.execute("SELECT reminders_sent FROM post_state WHERE source_id = 1").fetchone()[0] == n
    assert "DUE TODAY" in s.sent[0][1].text and "DUE TODAY" in s.sent[1][1].text
    res = digest.deliver(conn, "daily", FRI + 2 * DAY, sender=s, env=ENV)     # third morning
    third = s.sent[2][1]
    assert "DUE TODAY" not in third.text and "DECIDE: KEEP OR DISMISS?" in third.text and "Ignored one post about RAG" in third.text
    assert third.reminded_ids == []                                           # Decide does not count further
    assert conn.execute("SELECT reminders_sent FROM post_state WHERE source_id = 1").fetchone()[0] == 2
    assert [r["source_id"] for r in service.list_decide(conn, FRI + 2 * DAY)] == [1]
    assert conn.execute("SELECT status FROM post_state WHERE source_id = 1").fetchone()[0] == "open"   # never auto-dismissed


def test_any_user_action_resets_the_counter_but_automatic_ones_do_not(conn):
    add_post(conn, 1, "learning", due=FRI - DAY, reminders=2)
    conn.execute("INSERT INTO actions (source_id, action, destination, created_at) VALUES (1, 'archive', 'automatic: x', ?)", (FRI,))
    assert conn.execute("SELECT reminders_sent FROM post_state WHERE source_id = 1").fetchone()[0] == 2
    service.record_action(conn, 1, "apply_pack_copied", destination="clipboard", now=FRI)   # an action by the user
    assert conn.execute("SELECT reminders_sent FROM post_state WHERE source_id = 1").fetchone()[0] == 0
    add_post(conn, 2, "job", due=FRI - DAY, reminders=2, published=FRI - DAY)
    service.set_job_status(conn, 2, "in_progress", now=FRI)
    assert conn.execute("SELECT reminders_sent FROM post_state WHERE source_id = 2").fetchone()[0] == 0


def test_keep_resets_counter_pushes_due_seven_days_and_leaves_decide(conn):
    add_post(conn, 1, "learning", due=FRI - 5 * DAY, reminders=2)
    add_post(conn, 2, "learning", due=FRI + 3 * DAY, reminders=2)
    service.keep_post(conn, 1, now=FRI)
    service.keep_post(conn, 2, now=FRI)
    r = {x["source_id"]: x for x in conn.execute("SELECT * FROM post_state")}
    assert r[1]["reminders_sent"] == 0 and r[1]["due_at"] == FRI + 7 * DAY and r[1]["status"] == "open"   # overdue: from today
    assert r[2]["due_at"] == FRI + 10 * DAY                                                               # future: +7 from its date
    assert service.list_decide(conn, FRI) == []
    assert digest.due_items(conn, FRI) == []                      # not nagging tomorrow
    assert "keep" not in {d["action"] for d in service.list_done(conn)}      # not clutter in the Done log
    assert service.home_counts(conn, FRI)["done_this_week"] == 0


def test_dismiss_from_decide_keeps_post_searchable(conn):
    add_post(conn, 1, "learning", due=FRI - DAY, reminders=2, title="Quantum widgets explained")
    service.record_action(conn, 1, "dismiss", now=FRI)
    row = conn.execute("SELECT status, reminders_sent FROM post_state WHERE source_id = 1").fetchone()
    assert (row["status"], row["reminders_sent"]) == ("dismissed", 0)
    assert service.list_decide(conn, FRI) == []
    hits = search.search(conn, "quantum")
    assert any(h["id"] == 1 for h in hits)


# --- Sunday summary -------------------------------------------------------------------------

def test_weekly_goes_out_sunday_evening_once(conn):
    add_post(conn, 1, "learning", due=FRI + DAY)
    s = Sender()
    assert digest.deliver(conn, "weekly", SUN_EVENING - 3600, sender=s, env=ENV)["status"] == "not_due"      # 17:05
    assert digest.deliver(conn, "weekly", ist(2026, 11, 28, 18, 5), sender=s, env=ENV)["status"] == "not_due"  # Saturday
    assert digest.deliver(conn, "weekly", SUN_EVENING, sender=s, env=ENV)["status"] == "sent"
    assert digest.deliver(conn, "weekly", SUN_EVENING + 60, sender=s, env=ENV)["status"] == "already_done"
    assert len(s.sent) == 1


def test_weekly_numbers(conn):
    add_post(conn, 1, "learning", due=SUN_EVENING + DAY, saved=SUN_EVENING - DAY)
    add_post(conn, 2, "learning", due=SUN_EVENING + DAY, saved=SUN_EVENING - 2 * DAY)
    add_post(conn, 3, "job", due=SUN_EVENING + DAY, saved=SUN_EVENING - 20 * DAY, published=SUN_EVENING - 3 * DAY)
    add_post(conn, 4, "tool", due=SUN_EVENING + DAY, saved=SUN_EVENING - 30 * DAY)
    add_post(conn, 5, "job", due=SUN_EVENING + DAY, saved=SUN_EVENING - 30 * DAY, published=SUN_EVENING - 3 * DAY)
    t = SUN_EVENING - DAY
    service.record_action(conn, 1, "file_to_prep", destination="Prep: RAG", now=t)
    service.record_action(conn, 2, "dismiss", now=t)
    service.set_job_status(conn, 3, "applied", now=t)
    service.set_job_status(conn, 5, "deferred", now=t)
    conn.execute("INSERT INTO actions (source_id, action, destination, created_at) VALUES (4, 'archive', 'automatic: old', ?)", (t,))
    service.record_action(conn, 4, "archive", now=t)
    conn.commit()
    w = digest.week_numbers(conn, SUN_EVENING)
    assert (w["added"], w["done"], w["dismissed"], w["applied"], w["deferred"]) == (2, 1, 1, 1, 1)
    assert (w["archived"], w["archived_auto"]) == (2, 1)
    d = digest.build_weekly(conn, SUN_EVENING, "https://x")
    assert "WEEK IN NUMBERS" in d.text and "Jobs applied: 1" in d.text and "OPEN NOW" in d.text


# --- hand-off: notes file, idempotent ------------------------------------------------------------

def test_save_to_notes_is_idempotent_and_records_once(conn, tmp_path):
    add_post(conn, 1, "post_idea", due=FRI + DAY, title="An idea worth writing")
    notes = tmp_path / "notes.md"
    service.save_to_notes(conn, 1, path=str(notes), now=FRI)
    service.save_to_notes(conn, 1, path=str(notes), now=FRI + 5)
    assert notes.read_text(encoding="utf-8").count("## An idea worth writing") == 1
    assert [a["action"] for a in service.list_done(conn)].count("save_notes") == 1
    assert service.has_action(conn, 1, "save_notes")
    assert b"## An idea worth writing" in service.read_notes(str(notes))
    assert service.read_notes(str(tmp_path / "missing.md")) is None
    service.move_back_to_inbox(conn, 1, now=FRI + 10)                  # undone: may be saved again
    assert not service.has_action(conn, 1, "save_notes")


def test_dry_run_script_reads_a_copy_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from contentpilot import db
    from scripts import send_digest
    add_post(db.get_connection(), 1, "learning", due=FRI - DAY, title="Visible in dry run")
    live = db._DB_PATH
    before = open(live, "rb").read()
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("dry run must not open SMTP"))
    rc = send_digest.main(["--dry-run", "--db", live, "--at", "2026-11-27 07:30"])
    out = capsys.readouterr().out
    assert rc == 0 and "Visible in dry run" in out and "nothing sent" in out
    assert open(live, "rb").read() == before or sqlite3.connect(live).execute(
        "SELECT COUNT(*) FROM digest_log").fetchone()[0] == 0
    db._DB_PATH = live


# --- hand-off: PostPilot link, idempotent, independent of notes --------------------------------------

def test_send_to_postpilot_is_idempotent_encoded_and_independent_of_notes(conn, tmp_path):
    from urllib.parse import parse_qs, urlparse
    add_post(conn, 1, "post_idea", due=FRI + DAY, title="Why TPMs should treat AI as a thinking partner & more")
    conn.execute("UPDATE post_state SET suggested_action = 'React: contrarian take, with data' WHERE source_id = 1")
    first = service.send_to_postpilot(conn, 1, now=FRI)
    again = service.send_to_postpilot(conn, 1, now=FRI + 5)
    assert first["already_sent"] is False and again["already_sent"] is True
    assert [a["action"] for a in service.list_done(conn)].count("send_postpilot") == 1
    u = urlparse(first["url"])
    assert (u.scheme, u.netloc, u.path) == ("http", "100.78.225.111:4000", "/")
    q = parse_qs(u.query)
    assert q["tab"] == ["approvals"]
    assert q["idea"][0].startswith("Why TPMs should treat AI as a thinking partner & more. Angle: contrarian take, with data")
    assert "&more" not in first["url"]                                  # the & is encoded, not a new parameter
    # both on the same post, each recorded once
    service.save_to_notes(conn, 1, path=str(tmp_path / "notes.md"), now=FRI + 10)
    service.save_to_notes(conn, 1, path=str(tmp_path / "notes.md"), now=FRI + 11)
    acts = [a["action"] for a in service.list_done(conn) if a["source_id"] == 1]
    assert sorted(acts) == ["save_notes", "send_postpilot"]


def test_postpilot_idea_is_cut_with_a_flag_when_too_long(conn):
    add_post(conn, 1, "post_idea", due=FRI + DAY, title="T" * 40)
    conn.execute("UPDATE post_state SET suggested_action = ? WHERE source_id = 1", ("angle " * 200,))
    idea, cut = service.postpilot_idea(conn, 1)
    assert cut and len(idea) <= service.POSTPILOT_IDEA_MAX and idea.endswith("…")
    url, flag = service.postpilot_link(conn, 1)
    assert flag and url.endswith("&cut=1") and len(url) < 2200
    res = service.send_to_postpilot(conn, 1, now=FRI)
    assert res["shortened"] is True
    add_post(conn, 2, "post_idea", due=FRI + DAY, title="A short idea about something")
    assert service.postpilot_idea(conn, 2)[1] is False


def test_postpilot_url_setting(conn, monkeypatch):
    add_post(conn, 1, "post_idea", due=FRI + DAY)
    monkeypatch.setenv("POSTPILOT_UI_URL", "http://pp.example:4000/")
    assert service.postpilot_link(conn, 1)[0].startswith("http://pp.example:4000/?tab=approvals&idea=")


def test_sprint5_test_post_script_creates_and_removes(conn):
    from scripts import v3_sprint5_test_post as t
    sid = t.create(conn)
    t.create(conn)                                           # re-running resets it, never duplicates
    assert [r["source_id"] for r in service.list_decide(conn, FRI + 400 * DAY)] == [sid]
    service.keep_post(conn, sid, now=FRI)
    t.create(conn)                                           # back to 2 reminders for another try
    assert len(service.list_decide(conn, FRI)) == 1
    assert t.remove(conn) and not t.remove(conn)
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
