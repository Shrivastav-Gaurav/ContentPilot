"""Copilot operations: backfill, triage run, rules, actions, inbox and Done log.

Every function takes an open sqlite3 connection (row_factory=sqlite3.Row) and
leaves commit to the caller, so a whole run is one transaction.
"""
import time

from contentpilot.copilot import rules, search, triage
from contentpilot.copilot.parsing import parse_published_at, parse_publisher, parse_title, body_text

# What each action does to the post's status. Anything else -> 'done'.
_ACTION_STATUS = {"read": "read", "park": "parked", "dismiss": "dismissed", "archive": "archived"}
# Actions that record work but leave the post where it is.
_NO_STATUS_CHANGE = {"apply_pack_copied", "job_status"}


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

def backfill_facts(conn) -> dict:
    """Fill published_at, title and publisher from the fetched text where missing."""
    counts = {"published_at": 0, "title": 0, "publisher": 0}
    rows = conn.execute("SELECT id, raw_text, published_at, title, author_id FROM sources").fetchall()
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

def spent_today(conn, now: int | None = None) -> float:
    start = _now(now) - _now(now) % rules.DAY
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM outputs WHERE created_at >= ?", (start,)).fetchone()
    return float(row["c"])


def _untriaged(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.title, s.raw_text, s.intent, s.saved_at, s.fetched_at, s.published_at, a.name AS publisher "
        "FROM sources s LEFT JOIN authors a ON a.id = s.author_id "
        "LEFT JOIN post_state ps ON ps.source_id = s.id "
        "WHERE ps.source_id IS NULL AND s.raw_text IS NOT NULL AND TRIM(s.raw_text) != '' "
        "ORDER BY s.id"
    ).fetchall()
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
               daily_cap_usd: float = 0.10, batch_size: int = triage.BATCH_SIZE) -> dict:
    """Triage every post without a post_state row. Cached posts cost nothing."""
    now = _now(now)
    rule_cfg = load_rules(conn)
    stats = {"cached": 0, "llm_calls": 0, "triaged": 0, "cost_usd": 0.0, "stopped_by_cap": False, "errors": 0}
    pending: list[dict] = []
    for post in _untriaged(conn):
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
        out.append(d)
    return out


def list_done(conn, limit: int = 100) -> list[dict]:
    """Done log: latest actions first, with the post they belong to."""
    rows = conn.execute(
        "SELECT ac.*, s.title, s.canonical_url, ps.status AS post_status "
        "FROM actions ac JOIN sources s ON s.id = ac.source_id "
        "LEFT JOIN post_state ps ON ps.source_id = ac.source_id "
        "WHERE ac.action != 'move_back' ORDER BY ac.created_at DESC, ac.id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


def status_counts(conn) -> dict:
    return {r["k"]: r["n"] for r in conn.execute(
        "SELECT COALESCE(post_type, '?') || ':' || status AS k, COUNT(*) AS n FROM post_state GROUP BY 1")}
