"""Copilot operations: backfill, triage run, rules, actions, inbox and Done log.

Every function takes an open sqlite3 connection (row_factory=sqlite3.Row) and
leaves commit to the caller, so a whole run is one transaction.
"""
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Callable

from contentpilot import config
from contentpilot.copilot import fit, prep, rules, search, triage
from contentpilot.copilot.resume import Resume
from contentpilot.copilot.spend import DailyCapReached, spent_today  # noqa: F401  (re-exported)
from contentpilot.copilot.resume import load_resume as _load_resume_from
from contentpilot.copilot.topics import PREP_TOPICS
from contentpilot.copilot.parsing import (body_text, display_title, parse_published_at, parse_publisher,
                                          parse_title)

# What each action does to the post's status. Anything else -> 'done'.
_ACTION_STATUS = {"read": "read", "park": "parked", "dismiss": "dismissed", "archive": "archived"}
# Actions that record work but leave the post where it is.
_NO_STATUS_CHANGE = {"apply_pack_copied", "job_status"}


@contextmanager
def transaction():
    """For pages: one connection, committed on success, rolled back on error."""
    from contentpilot.db import get_connection
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


def load_rules(conn) -> dict:
    out = {}
    for row in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'due.%'"):
        try:
            out[row["key"][4:]] = int(row["value"])
        except (TypeError, ValueError):
            pass
    return out


# --- 1. facts without an LLM -------------------------------------------------

def backfill_facts(conn, source_ids: list[int] | None = None) -> dict:
    """Fill published_at, title and publisher from the fetched text where missing."""
    counts = {"published_at": 0, "title": 0, "publisher": 0}
    sql = "SELECT id, raw_text, published_at, title, author_id FROM sources"
    params: list = []
    if source_ids is not None:
        if not source_ids:
            return counts
        sql += f" WHERE id IN ({','.join('?' * len(source_ids))})"
        params = list(source_ids)
    rows = conn.execute(sql, params).fetchall()
    for r in rows:
        if r["published_at"] is None and (ts := parse_published_at(r["raw_text"])):
            conn.execute("UPDATE sources SET published_at = ? WHERE id = ?", (ts, r["id"]))
            counts["published_at"] += 1
        if not r["title"] and (t := parse_title(r["raw_text"])):
            conn.execute("UPDATE sources SET title = ? WHERE id = ?", (t[:300], r["id"]))
            counts["title"] += 1
        if r["author_id"] is None and (name := parse_publisher(r["raw_text"])):
            existing = conn.execute("SELECT id FROM authors WHERE name = ?", (name,)).fetchone()
            author_id = existing["id"] if existing else conn.execute(
                "INSERT INTO authors (name) VALUES (?)", (name,)
            ).lastrowid
            conn.execute("UPDATE sources SET author_id = ? WHERE id = ?", (author_id, r["id"]))
            counts["publisher"] += 1
    return counts


# --- 2. triage ---------------------------------------------------------------



def _untriaged(conn, only_ids: list[int] | None = None) -> list[dict]:
    sql = (
        "SELECT s.id, s.title, s.raw_text, s.intent, s.saved_at, s.fetched_at, s.published_at, a.name AS publisher "
        "FROM sources s LEFT JOIN authors a ON a.id = s.author_id "
        "LEFT JOIN post_state ps ON ps.source_id = s.id "
        "WHERE ps.source_id IS NULL AND s.raw_text IS NOT NULL AND TRIM(s.raw_text) != ''"
    )
    params: list = []
    if only_ids is not None:
        if not only_ids:
            return []
        sql += f" AND s.id IN ({','.join('?' * len(only_ids))})"
        params = list(only_ids)
    rows = conn.execute(sql + " ORDER BY s.id", params).fetchall()
    return [dict(r) for r in rows]


def _apply_triage(conn, post: dict, item: triage.TriageItem, now: int, rule_cfg: dict) -> str:
    saved_at = post["saved_at"] or post["fetched_at"] or now
    decision = rules.initial_decision(item.post_type, saved_at, post["published_at"], now, rule_cfg)
    conn.execute(
        "INSERT OR REPLACE INTO post_state (source_id, post_type, status, summary, suggested_action, role_title, "
        "due_at, triage_version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (post["id"], item.post_type, decision.status, item.summary, item.suggested_action, item.role_title,
         decision.due_at, triage.PROMPT_VERSION, now),
    )
    if decision.status in ("archived", "dismissed"):
        conn.execute(
            "INSERT INTO actions (source_id, action, destination, created_at) VALUES (?, ?, ?, ?)",
            (post["id"], {"archived": "archive", "dismissed": "dismiss"}[decision.status],
             f"automatic: {decision.reason}", now),
        )
    if item.post_type == "job":
        conn.execute("INSERT OR IGNORE INTO job_tracking (source_id, updated_at) VALUES (?, ?)", (post["id"], now))
    search.index_source(conn, post["id"])
    return decision.status


def run_triage(conn, llm: triage.LLM = triage.openrouter_llm, now: int | None = None,
               daily_cap_usd: float = 0.10, batch_size: int = triage.BATCH_SIZE,
               only_ids: list[int] | None = None) -> dict:
    """Triage every post without a post_state row (or just only_ids). Cached posts cost nothing."""
    now = _now(now)
    rule_cfg = load_rules(conn)
    stats = {"cached": 0, "llm_calls": 0, "triaged": 0, "cost_usd": 0.0, "stopped_by_cap": False, "errors": 0}
    pending: list[dict] = []
    for post in _untriaged(conn, only_ids):
        post["body"] = body_text(post["raw_text"])
        post["key"] = triage.cache_key(post["id"], post["raw_text"], post["intent"])
        cached = triage.load_cached(conn, post["key"])
        if cached:
            _apply_triage(conn, post, cached, now, rule_cfg)
            stats["cached"] += 1
            stats["triaged"] += 1
        else:
            pending.append(post)

    for i in range(0, len(pending), batch_size):
        if spent_today(conn, now) >= daily_cap_usd:
            stats["stopped_by_cap"] = True
            break
        chunk = pending[i:i + batch_size]
        try:
            items, cost = triage.triage_batch(chunk, llm)
        except triage.TriageError:
            stats["errors"] += len(chunk)
            continue
        stats["llm_calls"] += 1
        stats["cost_usd"] += cost
        per_post = cost / len(chunk) if chunk else 0.0
        for post in chunk:
            item = items[post["id"]]
            triage.store(conn, post["key"], item, per_post)
            _apply_triage(conn, post, item, now, rule_cfg)
            stats["triaged"] += 1
    return stats


# --- 3. rules over time ------------------------------------------------------

def apply_daily_rules(conn, now: int | None = None) -> dict:
    """Archive open job posts that crossed the age limit; wake deferred jobs."""
    now = _now(now)
    rule_cfg = load_rules(conn)
    archived = 0
    for r in conn.execute(
        "SELECT ps.source_id, ps.post_type, ps.status, s.published_at FROM post_state ps "
        "JOIN sources s ON s.id = ps.source_id WHERE ps.status = 'open' AND ps.post_type = 'job'"
    ).fetchall():
        if rules.should_auto_archive(r["post_type"], r["status"], r["published_at"], now, rule_cfg):
            conn.execute("UPDATE post_state SET status = 'archived', updated_at = ? WHERE source_id = ?",
                         (now, r["source_id"]))
            conn.execute("INSERT INTO actions (source_id, action, destination, created_at) VALUES (?, 'archive', ?, ?)",
                         (r["source_id"], f"automatic: job post older than {rule_cfg.get('job_archive_age', 90)} days", now))
            archived += 1
    woken = conn.execute(
        "UPDATE post_state SET snoozed_until = NULL, updated_at = ? "
        "WHERE snoozed_until IS NOT NULL AND snoozed_until <= ?", (now, now)
    ).rowcount
    return {"archived": archived, "woken": woken}


# --- 4. actions (always the user's choice) -----------------------------------

def record_action(conn, source_id: int, action: str, destination: str | None = None,
                  note: str | None = None, now: int | None = None) -> None:
    """Record an action. A post can carry several; the latest decides its status."""
    now = _now(now)
    conn.execute(
        "INSERT INTO actions (source_id, action, destination, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (source_id, action, destination, note, now),
    )
    if action not in _NO_STATUS_CHANGE:
        conn.execute("UPDATE post_state SET status = ?, updated_at = ? WHERE source_id = ?",
                     (_ACTION_STATUS.get(action, "done"), now, source_id))
    search.index_source(conn, source_id)


def move_back_to_inbox(conn, source_id: int, now: int | None = None) -> None:
    """Undo from the Done log: post is open again with a fresh due date; history kept."""
    now = _now(now)
    row = conn.execute("SELECT post_type FROM post_state WHERE source_id = ?", (source_id,)).fetchone()
    due = rules.reopen_due(row["post_type"] if row else None, now, load_rules(conn))
    conn.execute(
        "UPDATE post_state SET status = 'open', due_at = ?, reminders_sent = 0, snoozed_until = NULL, "
        "updated_at = ? WHERE source_id = ?", (due, now, source_id)
    )
    conn.execute("UPDATE actions SET undone_at = ? WHERE source_id = ? AND undone_at IS NULL", (now, source_id))
    conn.execute("INSERT INTO actions (source_id, action, destination, created_at, undone_at) "
                 "VALUES (?, 'move_back', 'Inbox', ?, ?)", (source_id, now, now))


def set_job_status(conn, source_id: int, user_status: str, note: str | None = None,
                   deferred_until: int | None = None, now: int | None = None) -> None:
    """Only the user sets a job's status. Copying the Apply pack never does."""
    if user_status not in rules.JOB_USER_STATUSES:
        raise ValueError(f"unknown job status {user_status!r}")
    now = _now(now)
    if user_status == "deferred" and deferred_until is None:
        deferred_until = now + rules.DEFAULT_RULES["deferred_default"] * rules.DAY
    conn.execute(
        "INSERT INTO job_tracking (source_id, user_status, note, deferred_until, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET user_status = excluded.user_status, note = excluded.note, "
        "deferred_until = excluded.deferred_until, updated_at = excluded.updated_at",
        (source_id, user_status, note, deferred_until if user_status == "deferred" else None, now),
    )
    record_action(conn, source_id, "job_status", destination=user_status, note=note, now=now)
    if user_status in ("applied", "not_a_fit"):
        conn.execute("UPDATE post_state SET status = 'done', updated_at = ? WHERE source_id = ?", (now, source_id))
    elif user_status == "deferred":
        conn.execute("UPDATE post_state SET status = 'open', snoozed_until = ?, due_at = ?, updated_at = ? "
                     "WHERE source_id = ?", (deferred_until, deferred_until, now, source_id))


def mark_apply_pack_copied(conn, source_id: int, now: int | None = None) -> None:
    now = _now(now)
    conn.execute(
        "INSERT INTO job_tracking (source_id, pack_copied_at, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET pack_copied_at = excluded.pack_copied_at, updated_at = excluded.updated_at",
        (source_id, now, now),
    )
    record_action(conn, source_id, "apply_pack_copied", destination="clipboard", now=now)


# --- 5. reading ----------------------------------------------------------------

def list_inbox(conn, now: int | None = None, post_type: str | None = None) -> list[dict]:
    now = _now(now)
    sql = (
        "SELECT ps.*, s.canonical_url, s.title, s.published_at, a.name AS publisher, "
        "jt.user_status, jt.note AS job_note "
        "FROM post_state ps JOIN sources s ON s.id = ps.source_id "
        "LEFT JOIN authors a ON a.id = s.author_id LEFT JOIN job_tracking jt ON jt.source_id = s.id "
        "WHERE ps.status = 'open' AND (ps.snoozed_until IS NULL OR ps.snoozed_until <= ?)"
    )
    params: list = [now]
    if post_type:
        sql += " AND ps.post_type = ?"
        params.append(post_type)
    sql += " ORDER BY ps.due_at IS NULL, ps.due_at ASC"
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["overdue_days"] = max(0, (now - d["due_at"]) // rules.DAY) if d["due_at"] else 0
        d["display_title"] = display_title(d["title"], d["summary"], d["publisher"])
        out.append(d)
    return out


def list_done(conn, limit: int = 100, action: str | None = None) -> list[dict]:
    """Done log: latest actions first, with the post they belong to.

    Each row also gets display_title and automatic (True for rule-made actions)."""
    sql = (
        "SELECT ac.*, s.title, s.canonical_url, ps.status AS post_status, ps.summary, ps.post_type, "
        "a.name AS publisher "
        "FROM actions ac JOIN sources s ON s.id = ac.source_id "
        "LEFT JOIN post_state ps ON ps.source_id = ac.source_id "
        "LEFT JOIN authors a ON a.id = s.author_id "
        "WHERE ac.action != 'move_back'"
    )
    params: list = []
    if action:
        sql += " AND ac.action = ?"
        params.append(action)
    sql += " ORDER BY ac.created_at DESC, ac.id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["display_title"] = display_title(d["title"], d["summary"], d["publisher"])
        d["automatic"] = (d["destination"] or "").startswith("automatic:")
        out.append(d)
    return out


def done_action_types(conn) -> list[str]:
    """Distinct action names in the Done log, for the filter."""
    return [r["action"] for r in conn.execute(
        "SELECT DISTINCT action FROM actions WHERE action != 'move_back' ORDER BY action")]


def status_counts(conn) -> dict:
    return {r["k"]: r["n"] for r in conn.execute(
        "SELECT COALESCE(post_type, '?') || ':' || status AS k, COUNT(*) AS n FROM post_state GROUP BY 1")}


def get_post(conn, source_id: int) -> dict | None:
    """One post with its triage state, for showing a row after an action."""
    row = conn.execute(
        "SELECT s.id AS source_id, s.canonical_url, s.title, s.published_at, s.source_type, s.intent, "
        "a.name AS publisher, ps.post_type, ps.status, ps.summary, ps.suggested_action, ps.due_at "
        "FROM sources s LEFT JOIN authors a ON a.id = s.author_id "
        "LEFT JOIN post_state ps ON ps.source_id = s.id WHERE s.id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["display_title"] = display_title(d["title"], d["summary"], d["publisher"])
    return d


def home_counts(conn, now: int | None = None, tz=None) -> dict:
    """Home page numbers: open posts by type, overdue (due before today), done this week."""
    now = _now(now)
    open_by_type = {t: 0 for t in rules.POST_TYPES if t != "noise"}
    for r in conn.execute(
        "SELECT post_type, COUNT(*) AS n FROM post_state WHERE status = 'open' "
        "AND (snoozed_until IS NULL OR snoozed_until <= ?) GROUP BY post_type", (now,)
    ):
        open_by_type[r["post_type"] or "?"] = r["n"]
    overdue = conn.execute(
        "SELECT COUNT(*) AS n FROM post_state WHERE status = 'open' AND due_at IS NOT NULL AND due_at < ? "
        "AND (snoozed_until IS NULL OR snoozed_until <= ?)", (rules.start_of_day(now, tz), now)
    ).fetchone()["n"]
    done_week = conn.execute(
        "SELECT COUNT(*) AS n FROM actions WHERE created_at >= ? AND undone_at IS NULL "
        "AND action NOT IN ('move_back', 'apply_pack_copied', 'job_status') "
        "AND COALESCE(destination, '') NOT LIKE 'automatic:%'", (rules.start_of_week(now, tz),)
    ).fetchone()["n"]
    return {"open_by_type": open_by_type, "open_total": sum(open_by_type.values()),
            "overdue": overdue, "done_this_week": done_week}


# --- 6. new posts and the notes file ------------------------------------------

def add_link(conn, url: str, intent: str | None = None, fetch: Callable | None = None,
             llm: triage.LLM = triage.openrouter_llm, now: int | None = None,
             daily_cap_usd: float = 0.10) -> dict:
    """Fetch a link, save it with the user's intent, and triage just that post.

    Call with no pending writes on conn: the source is saved through storage on its
    own connection. A link that is already triaged is not sent to the LLM again.
    Returns {"source_id", "new", "triage": stats or None, "post": get_post(...)}.
    """
    from contentpilot.storage import save_source
    if fetch is None:
        from contentpilot.fetchers.router import fetch
    now = _now(now)
    intent = (intent or "").strip() or None

    raw = fetch(url.strip())
    existing = conn.execute(
        "SELECT s.id, ps.source_id AS triaged FROM sources s LEFT JOIN post_state ps ON ps.source_id = s.id "
        "WHERE s.canonical_url = ?", (raw.canonical_url,)
    ).fetchone()
    if existing and existing["triaged"]:
        if intent:
            conn.execute("UPDATE sources SET intent = COALESCE(intent, ?) WHERE id = ?", (intent, existing["id"]))
        return {"source_id": existing["id"], "new": False, "triage": None, "post": get_post(conn, existing["id"])}

    source_id = save_source(raw)
    conn.execute("UPDATE sources SET intent = ?, saved_at = COALESCE(saved_at, ?) WHERE id = ?",
                 (intent, now, source_id))
    backfill_facts(conn, [source_id])
    stats = run_triage(conn, llm=llm, now=now, daily_cap_usd=daily_cap_usd, only_ids=[source_id])
    search.index_source(conn, source_id)
    return {"source_id": source_id, "new": True, "triage": stats, "post": get_post(conn, source_id)}


def notes_path() -> str:
    return os.path.join(config.DATA_DIR, "notes.md")


def save_to_notes(conn, source_id: int, path: str | None = None, now: int | None = None) -> str:
    """Append the post's title, link and suggested angle to the notes file; record the action."""
    now = _now(now)
    path = path or notes_path()
    post = get_post(conn, source_id)
    if post is None:
        raise ValueError(f"no post {source_id}")
    angle = (post["suggested_action"] or "").strip()
    if angle.lower().startswith("react:"):
        angle = angle[len("react:"):].strip()
    entry = (
        f"\n## {post['display_title']}\n"
        f"- Link: {post['canonical_url']}\n"
        f"- Angle: {angle or '-'}\n"
        f"- Saved: {datetime.fromtimestamp(now):%Y-%m-%d}\n"
    )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    record_action(conn, source_id, "save_notes", destination=os.path.basename(path), now=now)
    return path


# --- 7. cached outputs (prep notes, answers, fit) ------------------------------

def _load_output(conn, kind: str, key: str, version: str) -> dict | None:
    row = conn.execute(
        "SELECT content_json, created_at FROM outputs WHERE kind = ? AND cache_key = ? AND prompt_version = ?",
        (kind, key, version),
    ).fetchone()
    if row is None:
        return None
    out = json.loads(row["content_json"])
    out["_created_at"] = row["created_at"]
    return out


def _store_output(conn, kind: str, key: str, version: str, content: dict, model: str | None,
                  cost_usd: float | None, now: int) -> None:
    """Insert or replace (Regenerate overwrites the saved answer)."""
    conn.execute(
        "INSERT OR REPLACE INTO outputs (kind, cache_key, prompt_version, content_json, model, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, key, version, json.dumps(content, ensure_ascii=False), model, cost_usd, now),
    )


def _post_for_llm(conn, source_id: int) -> dict:
    post = get_post(conn, source_id)
    if post is None:
        raise ValueError(f"no post {source_id}")
    raw = conn.execute("SELECT raw_text FROM sources WHERE id = ?", (source_id,)).fetchone()["raw_text"]
    post["body"] = body_text(raw)
    return post


# --- 8. resume -------------------------------------------------------------------

def load_resume(data_dir: str | None = None) -> Resume:
    """data/resume.md, else the newest data/*.docx; an old .doc gives a notice instead.
    Never log the returned text."""
    return _load_resume_from(data_dir or config.DATA_DIR)


# --- 9. Prep Notebook ----------------------------------------------------------------

ANSWER_KIND = "prep_answer"
ANSWER_VERSION = "answer_v1"


def seed_prep_topics(conn) -> int:
    """Add the 22 Prep topics once. Safe to call on every page load (no-op after the first)."""
    added = 0
    for order, name in enumerate(PREP_TOPICS):
        added += conn.execute("INSERT OR IGNORE INTO prep_topics (name, sort_order) VALUES (?, ?)",
                              (name, order)).rowcount
    return added


def list_prep_topics(conn) -> list[dict]:
    """Topics with note and saved-answer counts. No Chroma, no LLM."""
    answers: dict[int, int] = {}
    for r in conn.execute("SELECT json_extract(content_json, '$.topic_id') AS t, COUNT(*) AS n FROM outputs "
                          "WHERE kind = ? AND prompt_version = ? GROUP BY t", (ANSWER_KIND, ANSWER_VERSION)):
        if r["t"] is not None:
            answers[int(r["t"])] = r["n"]
    rows = conn.execute(
        "SELECT t.id, t.name, COUNT(n.id) AS note_count, "
        "SUM(CASE WHEN n.source_id IS NULL AND n.id IS NOT NULL THEN 1 ELSE 0 END) AS story_count "
        "FROM prep_topics t LEFT JOIN prep_notes n ON n.topic_id = t.id "
        "GROUP BY t.id ORDER BY t.sort_order IS NULL, t.sort_order, t.name"
    ).fetchall()
    return [{**dict(r), "answer_count": answers.get(r["id"], 0)} for r in rows]


def prep_coverage(conn) -> dict:
    """A topic is covered when it has 2+ notes (filed posts or your own stories)."""
    topics = list_prep_topics(conn)
    covered = [t for t in topics if t["note_count"] >= 2]
    total = len(topics)
    return {"covered": len(covered), "total": total, "pct": round(100 * len(covered) / total) if total else 0}


def get_prep_topic(conn, topic_id: int) -> dict | None:
    row = conn.execute("SELECT id, name FROM prep_topics WHERE id = ?", (topic_id,)).fetchone()
    return dict(row) if row else None


def list_prep_notes(conn, topic_id: int) -> list[dict]:
    """Notes in a topic, newest first, with their source (None for your own stories)."""
    rows = conn.execute(
        "SELECT n.id, n.source_id, n.text, n.created_at, s.canonical_url, s.title, a.name AS publisher, "
        "ps.summary FROM prep_notes n LEFT JOIN sources s ON s.id = n.source_id "
        "LEFT JOIN authors a ON a.id = s.author_id LEFT JOIN post_state ps ON ps.source_id = n.source_id "
        "WHERE n.topic_id = ? ORDER BY n.created_at DESC, n.id DESC", (topic_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["display_title"] = display_title(d["title"], d["summary"], d["publisher"]) if d["source_id"] else None
        out.append(d)
    return out


def add_story(conn, topic_id: int, text: str, now: int | None = None) -> int:
    """'My stories': your own STAR notes, saved with no source."""
    text = (text or "").strip()
    if not text:
        raise ValueError("story is empty")
    if get_prep_topic(conn, topic_id) is None:
        raise ValueError(f"no topic {topic_id}")
    return conn.execute("INSERT INTO prep_notes (topic_id, source_id, text, created_at) VALUES (?, NULL, ?, ?)",
                        (topic_id, text, _now(now))).lastrowid


def prep_topics_for_source(conn, source_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT DISTINCT t.id, t.name FROM prep_notes n JOIN prep_topics t ON t.id = n.topic_id "
        "WHERE n.source_id = ? ORDER BY t.sort_order", (source_id,))]


def _topic_ids_by_name(conn) -> dict[str, int]:
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM prep_topics")}


def file_to_prep(conn, source_id: int, llm: triage.LLM = triage.openrouter_llm, now: int | None = None) -> dict:
    """File a learning post: one cached Haiku call picks 1-2 topics and writes 2-4 notes.

    A post that already has notes, or a cached result, costs nothing.
    Returns {"topics": [names], "llm_calls": 0|1, "cost_usd": float}.
    """
    now = _now(now)
    seed_prep_topics(conn)
    calls, cost = 0, 0.0
    topics = [t["name"] for t in prep_topics_for_source(conn, source_id)]
    if not topics:
        key = str(source_id)
        cached = _load_output(conn, prep.KIND, key, prep.PROMPT_VERSION)
        if cached:
            cached.pop("_created_at", None)
            note = prep.PrepNote.model_validate(cached)
        else:
            note, cost = prep.generate(_post_for_llm(conn, source_id), llm)
            calls = 1
            _store_output(conn, prep.KIND, key, prep.PROMPT_VERSION, note.model_dump(),
                          config.openrouter_model(), cost, now)
        ids = _topic_ids_by_name(conn)
        text = prep.note_text(note)
        for name in note.topics:
            conn.execute("INSERT INTO prep_notes (topic_id, source_id, text, created_at) VALUES (?, ?, ?, ?)",
                         (ids[name], source_id, text, now))
        topics = list(note.topics)
    record_action(conn, source_id, "file_to_prep", destination="Prep: " + ", ".join(topics), now=now)
    return {"topics": topics, "llm_calls": calls, "cost_usd": cost}


def move_prep_notes(conn, source_id: int, from_topic_id: int, to_topic_id: int, now: int | None = None) -> None:
    """'Change topic': move a filed post's notes to another topic. No LLM call."""
    if from_topic_id == to_topic_id:
        return
    if get_prep_topic(conn, to_topic_id) is None:
        raise ValueError(f"no topic {to_topic_id}")
    already = conn.execute("SELECT 1 FROM prep_notes WHERE source_id = ? AND topic_id = ?",
                           (source_id, to_topic_id)).fetchone()
    if already:  # the post is already filed there too: drop the duplicate copy instead of doubling it
        conn.execute("DELETE FROM prep_notes WHERE source_id = ? AND topic_id = ?", (source_id, from_topic_id))
    else:
        conn.execute("UPDATE prep_notes SET topic_id = ? WHERE source_id = ? AND topic_id = ?",
                     (to_topic_id, source_id, from_topic_id))
    names = ", ".join(t["name"] for t in prep_topics_for_source(conn, source_id))
    conn.execute(
        "UPDATE actions SET destination = ? WHERE id = (SELECT MAX(id) FROM actions WHERE source_id = ? "
        "AND action = 'file_to_prep')", (f"Prep: {names}", source_id))
    search.index_source(conn, source_id)


def open_learning_ids(conn, now: int | None = None) -> list[int]:
    return [r["source_id"] for r in list_inbox(conn, now=now, post_type="learning")]


def file_all_learning(conn, llm: triage.LLM = triage.openrouter_llm, now: int | None = None,
                      daily_cap_usd: float = 0.10, on_progress: Callable[[int, int], None] | None = None) -> dict:
    """File every open learning post. Stops cleanly at the daily spend cap."""
    now = _now(now)
    ids = open_learning_ids(conn, now)
    stats = {"total": len(ids), "filed": 0, "llm_calls": 0, "cost_usd": 0.0, "errors": 0, "stopped_by_cap": False}
    for i, source_id in enumerate(ids):
        if spent_today(conn, now) >= daily_cap_usd:
            stats["stopped_by_cap"] = True
            break
        try:
            res = file_to_prep(conn, source_id, llm=llm, now=now)
        except Exception:
            stats["errors"] += 1
            continue
        stats["filed"] += 1
        stats["llm_calls"] += res["llm_calls"]
        stats["cost_usd"] += res["cost_usd"]
        if on_progress:
            on_progress(i + 1, len(ids))
    return stats


def question_key(question: str) -> str:
    return " ".join((question or "").split()).lower()


SPEND_KIND = "spend_log"


def _log_spend(conn, label: str, cost_usd: float | None, at: int) -> None:
    """Keep a cost in today's total when its output row is replaced or never written."""
    if cost_usd:
        conn.execute(
            "INSERT OR IGNORE INTO outputs (kind, cache_key, prompt_version, content_json, model, cost_usd, created_at) "
            "VALUES (?, ?, ?, '{}', ?, ?, ?)",
            (SPEND_KIND, f"{label}@{at}", ANSWER_VERSION, config.synthesis_model(), cost_usd, at),
        )


def ask_prep(conn, question: str, topic_id: int | None = None, retrieve_fn: Callable | None = None,
             synth_fn: Callable | None = None, regenerate: bool = False, now: int | None = None,
             daily_cap_usd: float = 0.10) -> dict | None:
    """Answer an interview question from the library (retriever + synthesizer, once).

    Saved in outputs(kind='prep_answer', cache_key=<lower-cased trimmed question>) with the
    call's cost, so asking again reopens it with 0 calls; only regenerate=True makes a new
    call. Before any new call the daily cap is checked: DailyCapReached is raised instead.
    Returns {"question", "topic_id", "answer": {...}, "asked_at", "cached"} or None
    when the library has nothing relevant.
    """
    key = question_key(question)
    if not key:
        raise ValueError("question is empty")
    now = _now(now)
    saved = _load_output(conn, ANSWER_KIND, key, ANSWER_VERSION)
    if saved and not regenerate:
        saved.pop("_created_at", None)
        return {**saved, "cached": True}
    spent = spent_today(conn, now)
    if spent >= daily_cap_usd:
        raise DailyCapReached(f"Today's API spend (${spent:.2f}) has reached the ${daily_cap_usd:.2f} cap.")
    if retrieve_fn is None:
        from contentpilot.rag.retriever import retrieve as retrieve_fn
    if synth_fn is None:
        from contentpilot.rag.synthesizer import synthesize as synth_fn
    chunks = retrieve_fn(question.strip())
    if not chunks:
        return None
    try:
        result = synth_fn(question.strip(), chunks)
    except Exception as exc:
        _log_spend(conn, f"failed:{key}", getattr(exc, "cost_usd", None), now)
        raise
    content = {
        "question": " ".join(question.split()),
        "topic_id": topic_id if topic_id is not None else (saved or {}).get("topic_id"),
        "answer": result.model_dump() if hasattr(result, "model_dump") else dict(result),
        "asked_at": now,
    }
    if saved:  # Regenerate replaces the row: keep the earlier call's cost on its own day
        old = conn.execute("SELECT cost_usd, created_at FROM outputs WHERE kind = ? AND cache_key = ? "
                           "AND prompt_version = ?", (ANSWER_KIND, key, ANSWER_VERSION)).fetchone()
        _log_spend(conn, f"replaced:{key}", old["cost_usd"], old["created_at"])
    _store_output(conn, ANSWER_KIND, key, ANSWER_VERSION, content, config.synthesis_model(),
                  getattr(result, "cost_usd", None), now)
    return {**content, "cached": False}


def list_prep_answers(conn, topic_id: int) -> list[dict]:
    """Saved answers filed under a topic, newest first. Reading them makes no calls."""
    rows = conn.execute(
        "SELECT cache_key, content_json, created_at FROM outputs WHERE kind = ? AND prompt_version = ? "
        "AND json_extract(content_json, '$.topic_id') = ? ORDER BY created_at DESC",
        (ANSWER_KIND, ANSWER_VERSION, topic_id),
    ).fetchall()
    return [{**json.loads(r["content_json"]), "key": r["cache_key"], "saved_at": r["created_at"]} for r in rows]


# --- 10. Jobs ----------------------------------------------------------------------

JOB_STATUS_LABELS = {"not_started": "Not started", "in_progress": "In progress", "applied": "Applied",
                     "deferred": "Deferred", "not_a_fit": "Not a fit"}

APPLY_INSTRUCTIONS = """INSTRUCTIONS
Using the job post and my resume above:
1. Tailor my resume to this job. Keep it to 2 pages. Keep every fact true: do not invent or
   inflate titles, dates, numbers, employers or skills.
2. List the keywords and skills from the job post that are missing from my resume.
3. Write a short cover note (under 150 words) for this role.
4. Suggest 3 questions I should ask the recruiter."""


def list_jobs(conn, resume_hash: str | None, now: int | None = None) -> list[dict]:
    """Job posts published within job_rank_max_age days (or undated) that are still open,
    shortlisted or being worked on, ranked by fit. Named employers rank above unnamed ones;
    unscored jobs come last. The top 3 not yet applied are flagged apply_this_week."""
    now = _now(now)
    max_age = {**rules.DEFAULT_RULES, **load_rules(conn)}["job_rank_max_age"]
    rows = conn.execute(
        "SELECT ps.source_id, ps.status, ps.summary, ps.role_title, ps.suggested_action, ps.due_at, ps.snoozed_until, "
        "s.title, s.canonical_url, s.published_at, a.name AS publisher, "
        "COALESCE(jt.user_status, 'not_started') AS user_status, jt.note AS job_note, jt.deferred_until, "
        "jt.pack_copied_at, EXISTS (SELECT 1 FROM actions ac WHERE ac.source_id = ps.source_id "
        "AND ac.action = 'shortlist' AND ac.undone_at IS NULL) AS shortlisted "
        "FROM post_state ps JOIN sources s ON s.id = ps.source_id LEFT JOIN authors a ON a.id = s.author_id "
        "LEFT JOIN job_tracking jt ON jt.source_id = ps.source_id "
        "WHERE ps.post_type = 'job' AND (s.published_at IS NULL OR s.published_at >= ?) "
        "AND ps.status NOT IN ('archived', 'dismissed') AND COALESCE(jt.user_status, 'not_started') != 'not_a_fit'",
        (now - max_age * rules.DAY,),
    ).fetchall()
    jobs = []
    for r in rows:
        d = dict(r)
        d["shortlisted"] = bool(d["shortlisted"])
        if not (d["status"] == "open" or d["shortlisted"] or d["user_status"] in ("in_progress", "applied", "deferred")):
            continue
        d["display_title"] = display_title(d["title"], d["summary"], d["publisher"])
        d["fit"] = None
        if resume_hash:
            cached = _load_output(conn, fit.KIND, fit.cache_key(d["source_id"], resume_hash), fit.PROMPT_VERSION)
            if cached:
                cached.pop("_created_at", None)
                d["fit"] = cached
        jobs.append(d)

    def rank_key(j):
        f = j["fit"]
        if f is None:
            return (2, 0, j["published_at"] is None, -(j["published_at"] or 0))
        return (0 if f.get("employer") else 1, -f["fit_score"], 0, 0)

    jobs.sort(key=rank_key)
    flagged = 0
    for i, j in enumerate(jobs, start=1):
        j["rank"] = i
        j["apply_this_week"] = False
        if flagged < 3 and j["fit"] and j["user_status"] != "applied":
            j["apply_this_week"] = True
            flagged += 1
    return jobs


def list_older_jobs(conn, now: int | None = None) -> list[dict]:
    """Open job posts published between job_rank_max_age and job_archive_age days ago
    (60-90 by default): too old to rank or score, not yet auto-archived."""
    now = _now(now)
    r = {**rules.DEFAULT_RULES, **load_rules(conn)}
    rows = conn.execute(
        "SELECT ps.source_id, ps.summary, ps.role_title, s.title, s.canonical_url, s.published_at, "
        "a.name AS publisher FROM post_state ps JOIN sources s ON s.id = ps.source_id "
        "LEFT JOIN authors a ON a.id = s.author_id "
        "WHERE ps.post_type = 'job' AND ps.status = 'open' AND s.published_at IS NOT NULL "
        "AND s.published_at < ? AND s.published_at >= ? ORDER BY s.published_at DESC",
        (now - r["job_rank_max_age"] * rules.DAY, now - r["job_archive_age"] * rules.DAY),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["display_title"] = display_title(d["title"], d["summary"], d["publisher"])
        d["age_days"] = (now - d["published_at"]) // rules.DAY
        out.append(d)
    return out


def jobs_archived_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM post_state WHERE post_type = 'job' AND status = 'archived'"
                        ).fetchone()["n"]


def score_jobs(conn, resume: Resume, llm: triage.LLM = triage.openrouter_llm, now: int | None = None,
               daily_cap_usd: float = 0.10) -> dict:
    """Score every listed job that has no fit for this resume version. One call per job, cached."""
    now = _now(now)
    stats = {"scored": 0, "llm_calls": 0, "cost_usd": 0.0, "errors": 0, "stopped_by_cap": False, "skipped": None}
    if not resume.ok:
        stats["skipped"] = resume.notice
        return stats
    for job in list_jobs(conn, resume.hash, now):
        if job["fit"] is not None:
            continue
        if spent_today(conn, now) >= daily_cap_usd:
            stats["stopped_by_cap"] = True
            break
        try:
            result, cost = fit.score(_post_for_llm(conn, job["source_id"]) | {"source_id": job["source_id"]},
                                     resume.text, llm)
        except Exception:
            stats["errors"] += 1
            continue
        stats["llm_calls"] += 1
        stats["cost_usd"] += cost
        _store_output(conn, fit.KIND, fit.cache_key(job["source_id"], resume.hash), fit.PROMPT_VERSION,
                      result.model_dump(), config.openrouter_model(), cost, now)
        conn.execute(
            "INSERT INTO job_tracking (source_id, fit_score, fit_reason, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET fit_score = excluded.fit_score, fit_reason = excluded.fit_reason, "
            "updated_at = excluded.updated_at",
            (job["source_id"], result.fit_score, "; ".join(result.reasons), now),
        )
        stats["scored"] += 1
    return stats


def unscored_jobs_count(conn, resume_hash: str | None, now: int | None = None) -> int:
    return sum(1 for j in list_jobs(conn, resume_hash, now) if j["fit"] is None)


def build_apply_pack(conn, source_id: int, resume: Resume) -> str:
    """Job post text + resume + instructions, as one block to paste into a chat."""
    if not resume.ok:
        raise ValueError(resume.notice or "no resume")
    post = _post_for_llm(conn, source_id)
    role = post.get("display_title") or ""
    return (f"=== JOB POST ===\n{role}\n{post['canonical_url']}\n\n{post['body'].strip()}\n\n"
            f"=== MY RESUME ===\n{resume.text.strip()}\n\n=== {APPLY_INSTRUCTIONS}")


def show_apply_pack(conn, source_id: int, resume: Resume, now: int | None = None) -> str:
    """Build the pack and record that it was copied. The job's status does not change."""
    pack = build_apply_pack(conn, source_id, resume)
    mark_apply_pack_copied(conn, source_id, now=now)
    return pack
