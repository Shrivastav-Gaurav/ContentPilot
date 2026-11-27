"""Daily email and Sunday summary. Built from the database only: no LLM, no API calls.

Times are Asia/Kolkata. The worker calls run_scheduled() every few minutes; the digest_log
primary key (kind, local day) makes sure each email goes out at most once per day, even across
restarts or two worker passes. Gmail settings come from the environment and are never logged.
"""
import html
import os
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Callable
from zoneinfo import ZoneInfo

from contentpilot.copilot import service
from contentpilot.copilot.rules import DAY, TYPE_LABELS, start_of_day

TZ = ZoneInfo("Asia/Kolkata")
DAILY_AT = (7, 30)               # every day
WEEKLY_AT = (6, 18, 0)           # weekday (Monday=0 ... Sunday=6), hour, minute
MAX_DUE_ITEMS = 10
TOP_JOBS = 3
RETRY_AFTER_SEC = 30 * 60        # a failed send is retried at most every 30 minutes
TYPE_ORDER = ("job", "learning", "post_idea", "tool")
REQUIRED_ENV = ("GMAIL_USER", "GMAIL_APP_PASSWORD", "DIGEST_TO", "APP_BASE_URL")


@dataclass
class Digest:
    kind: str                    # 'daily' | 'weekly'
    subject: str
    text: str
    html: str
    reminded_ids: list[int] = field(default_factory=list)   # counted as reminded once sent
    counts: dict = field(default_factory=dict)


# --- settings ---------------------------------------------------------------------

def mail_settings(env=None) -> tuple[dict | None, list[str]]:
    """(settings, []) when complete, else (None, names of the missing keys)."""
    env = os.environ if env is None else env
    vals = {k: (env.get(k) or "").strip() for k in REQUIRED_ENV}
    missing = [k for k, v in vals.items() if not v]
    if missing:
        return None, missing
    vals["APP_BASE_URL"] = vals["APP_BASE_URL"].rstrip("/")
    return vals, []


def send_gmail(settings: dict, digest: Digest) -> None:
    msg = EmailMessage()
    msg["Subject"] = digest.subject
    msg["From"] = settings["GMAIL_USER"]
    msg["To"] = settings["DIGEST_TO"]
    msg.set_content(digest.text)
    msg.add_alternative(digest.html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(settings["GMAIL_USER"], settings["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)


# --- building ---------------------------------------------------------------------

def local_day(now: int) -> str:
    return datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d")


def _age(row: dict, now: int) -> str:
    # Jobs go stale, so their age is when they were posted; everything else shows how long it has been waiting.
    use_posted = row.get("post_type") == "job" and row.get("published_at")
    ts = row["published_at"] if use_posted else (row.get("saved_at") or row.get("fetched_at"))
    if not ts:
        return "age unknown"
    days = max(0, (now - ts) // DAY)
    return ("posted " if use_posted else "saved ") + ("today" if days == 0 else f"{days}d ago")


def _due_text(due_at: int | None, now: int) -> str:
    if not due_at:
        return "no due date"
    days = (datetime.fromtimestamp(due_at, TZ).date() - datetime.fromtimestamp(now, TZ).date()).days
    return "due today" if days == 0 else f"overdue {-days}d"


def due_items(conn, now: int) -> list[dict]:
    """Due today or overdue, not yet ignored twice. Most overdue first, at most MAX_DUE_ITEMS."""
    end_of_today = start_of_day(now, TZ) + DAY
    rows = conn.execute(
        "SELECT ps.source_id, ps.post_type, ps.summary, ps.due_at, s.title, s.published_at, s.saved_at, s.fetched_at, "
        "a.name AS publisher FROM post_state ps JOIN sources s ON s.id = ps.source_id "
        "LEFT JOIN authors a ON a.id = s.author_id WHERE ps.status = 'open' AND ps.post_type IN "
        "('job', 'learning', 'post_idea', 'tool') AND ps.due_at IS NOT NULL AND ps.due_at < ? "
        "AND ps.reminders_sent < ? AND (ps.snoozed_until IS NULL OR ps.snoozed_until <= ?) "
        "ORDER BY ps.due_at ASC, ps.source_id ASC LIMIT ?",
        (end_of_today, service.IGNORED_REMINDER_LIMIT, now, MAX_DUE_ITEMS),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["display_title"] = service.display_title(d["title"], d["summary"], d["publisher"])
        out.append(d)
    return out


def top_jobs(conn, resume_hash: str | None, now: int) -> list[dict]:
    """Best existing fit scores among jobs from the last 60 days that are still to apply to."""
    if not resume_hash:
        return []
    jobs = [j for j in service.list_jobs(conn, resume_hash, now)
            if j["fit"] and j["user_status"] in ("not_started", "in_progress")]
    return jobs[:TOP_JOBS]   # list_jobs is already ranked by fit


def _link(base: str, kind: str, source_id: int) -> str:
    return f"{base}/Jobs" if kind == "job" else f"{base}/Inbox?post={source_id}"


def _line(r: dict, base: str, now: int, extra: str = "") -> tuple[str, str]:
    """(plain text line, html line) for one post."""
    title, who = r["display_title"], r.get("publisher") or "unknown publisher"
    meta = f"{who} · {_age(r, now)}" + (f" · {extra}" if extra else "")
    url = _link(base, r.get("post_type"), r["source_id"])
    return (f"- {title} ({meta})\n  {url}",
            f'<li><a href="{html.escape(url)}">{html.escape(title)}</a><br>'
            f'<span style="color:#666">{html.escape(meta)}</span></li>')


def _section(heading: str, entries: list[tuple[str, str]], note: str | None = None) -> tuple[str, str]:
    nl = chr(10)
    text = heading.upper() + nl + (note + nl if note else "") + nl.join(t for t, _ in entries)
    htm = (f'<h3 style="margin:18px 0 6px">{html.escape(heading)}</h3>' + (f"<p>{html.escape(note)}</p>" if note else "")
           + "<ul>" + "".join(h for _, h in entries) + "</ul>")
    return text, htm


def _jobs_part(conn, base: str, now: int, resume_hash: str | None):
    jobs = top_jobs(conn, resume_hash, now)
    if not jobs:
        return None, 0
    entries = [_line(j, base, now, f"fit {j['fit']['fit_score']}"
                     + (f" · {j['fit']['employer']}" if j["fit"].get("employer") else "")) for j in jobs]
    return ("jobs", *_section(f"Top {len(jobs)} jobs to apply to", entries)), len(jobs)


def _decide_part(conn, base: str, now: int):
    decide = service.list_decide(conn, now)[:MAX_DUE_ITEMS]
    if not decide:
        return None, 0
    entries = [_line(r, base, now, f"in {r['reminders_sent']} emails, no action") for r in decide]
    note = "Twice in the email with no action. Open the Inbox: Keep (back in 7 days) or Dismiss."
    return ("decide", *_section("Decide: keep or dismiss?", entries, note)), len(decide)


def _sections(conn, base: str, now: int, resume_hash: str | None):
    """The daily email's sections. Returns (parts, ids counted as reminded, counts)."""
    parts, reminded = [], []
    due = due_items(conn, now)
    if due:
        grouped = []
        for t in TYPE_ORDER:
            rows = [r for r in due if r["post_type"] == t]
            if rows:
                grouped.append((f"{TYPE_LABELS[t]} ({len(rows)})",
                                [_line(r, base, now, _due_text(r["due_at"], now)) for r in rows]))
        nl = chr(10)
        text = f"DUE TODAY / OVERDUE ({len(due)})" + nl + (nl * 2).join(
            h + nl + nl.join(t for t, _ in es) for h, es in grouped)
        htm = (f'<h3 style="margin:18px 0 6px">Due today / overdue ({len(due)})</h3>' + "".join(
            f'<p style="margin:10px 0 2px"><b>{html.escape(h)}</b></p><ul>' + "".join(x for _, x in es) + "</ul>"
            for h, es in grouped))
        parts.append(("due", text, htm))
        reminded = [r["source_id"] for r in due]
    jobs_part, n_jobs = _jobs_part(conn, base, now, resume_hash)
    decide_part, n_decide = _decide_part(conn, base, now)
    parts += [p for p in (jobs_part, decide_part) if p]
    return parts, reminded, {"due": len(due), "jobs": n_jobs, "decide": n_decide}


def _wrap(title: str, parts, base: str) -> tuple[str, str]:
    text = f"{title}\n{'=' * len(title)}\n\n" + "\n\n".join(p[1] for p in parts) + f"\n\nOpen ContentPilot: {base}/Inbox\n"
    htm = (f'<div style="font-family:Arial,sans-serif;max-width:640px"><h2 style="margin-bottom:0">{html.escape(title)}</h2>'
           + "".join(p[2] for p in parts)
           + f'<p style="margin-top:22px"><a href="{html.escape(base)}/Inbox">Open ContentPilot</a></p></div>')
    return text, htm


def build_daily(conn, now: int, base_url: str, resume_hash: str | None = None) -> Digest | None:
    """None when there is nothing due, no job worth applying to and nothing to decide."""
    parts, reminded, counts = _sections(conn, base_url.rstrip("/"), now, resume_hash)
    if not parts:
        return None
    d = datetime.fromtimestamp(now, TZ)
    bits = [f"{counts['due']} due" if counts["due"] else "", f"{counts['jobs']} jobs" if counts["jobs"] else "",
            f"{counts['decide']} to decide" if counts["decide"] else ""]
    title = f"ContentPilot · {d:%a} {d.day} {d:%b}"
    text, htm = _wrap(title, parts, base_url.rstrip("/"))
    return Digest("daily", f"{title}: " + ", ".join(b for b in bits if b), text, htm, reminded, counts)


def week_numbers(conn, now: int) -> dict:
    """The last 7 days, straight from the actions table and post_state."""
    since = now - 7 * DAY

    def n(sql, *params):
        return conn.execute(sql, params).fetchone()[0]

    base = "FROM actions WHERE created_at >= ? AND undone_at IS NULL"
    mine = "AND COALESCE(destination, '') NOT LIKE 'automatic:%'"
    return {
        "added": n("SELECT COUNT(*) FROM sources WHERE saved_at >= ?", since),
        "done": n(f"SELECT COUNT(DISTINCT source_id) {base} {mine} AND action NOT IN "
                  "('dismiss', 'archive', 'move_back', 'apply_pack_copied', 'job_status', 'keep')", since),
        "archived": n(f"SELECT COUNT(*) {base} AND action = 'archive'", since),
        "archived_auto": n(f"SELECT COUNT(*) {base} AND action = 'archive' AND destination LIKE 'automatic:%'", since),
        "dismissed": n(f"SELECT COUNT(*) {base} {mine} AND action = 'dismiss'", since),
        "applied": n(f"SELECT COUNT(*) {base} AND action = 'job_status' AND destination = 'applied'", since),
        "deferred": n(f"SELECT COUNT(*) {base} AND action = 'job_status' AND destination = 'deferred'", since),
        "open_by_type": service.home_counts(conn, now)["open_by_type"],
    }


def build_weekly(conn, now: int, base_url: str, resume_hash: str | None = None) -> Digest:
    base = base_url.rstrip("/")
    w = week_numbers(conn, now)
    auto = f" ({w['archived_auto']} automatic)" if w["archived_auto"] else ""
    rows = [("Added", w["added"]), ("Filed / done", w["done"]), ("Archived", f"{w['archived']}{auto}"),
            ("Dismissed", w["dismissed"]), ("Jobs applied", w["applied"]), ("Jobs deferred", w["deferred"])]
    open_types = [(TYPE_LABELS[t], c) for t, c in w["open_by_type"].items() if t in TYPE_LABELS and c]
    text = "WEEK IN NUMBERS\n" + "\n".join(f"- {k}: {v}" for k, v in rows)
    text += "\n\nOPEN NOW (" + str(sum(c for _, c in open_types)) + ")\n" + "\n".join(f"- {k}: {c}" for k, c in open_types)
    htm = ('<h3 style="margin:18px 0 6px">Week in numbers</h3><ul>' + "".join(f"<li>{k}: <b>{v}</b></li>" for k, v in rows)
           + f'</ul><h3 style="margin:18px 0 6px">Open now ({sum(c for _, c in open_types)})</h3><ul>'
           + "".join(f"<li>{k}: <b>{c}</b></li>" for k, c in open_types) + "</ul>")
    jobs_part, n_jobs = _jobs_part(conn, base, now, resume_hash)
    decide_part, n_decide = _decide_part(conn, base, now)
    parts = [("week", text, htm)] + [p for p in (jobs_part, decide_part) if p]
    d = datetime.fromtimestamp(now, TZ)
    title = f"ContentPilot weekly summary · {d.day} {d:%b}"
    t, h = _wrap(title, parts, base)
    return Digest("weekly", title, t, h, [], {"decide": n_decide, "jobs": n_jobs})


def build(kind: str, conn, now: int, base_url: str, resume_hash: str | None = None) -> Digest | None:
    return (build_daily if kind == "daily" else build_weekly)(conn, now, base_url, resume_hash)


# --- the once-per-day guard ---------------------------------------------------------

def claim(conn, kind: str, day: str, now: int, retry_after: int = RETRY_AFTER_SEC) -> bool:
    """True for exactly one caller per (kind, day). A failed send may be claimed again after retry_after."""
    cur = conn.execute("INSERT OR IGNORE INTO digest_log (kind, day, status, created_at, updated_at) "
                       "VALUES (?, ?, 'sending', ?, ?)", (kind, day, now, now))
    won = cur.rowcount == 1
    if not won:
        won = conn.execute("UPDATE digest_log SET status = 'sending', updated_at = ? WHERE kind = ? AND day = ? "
                           "AND status = 'failed' AND updated_at <= ?", (now, kind, day, now - retry_after)).rowcount == 1
    conn.commit()
    return won


def _finish(conn, kind: str, day: str, status: str, items: int, error: str | None, now: int) -> None:
    conn.execute("UPDATE digest_log SET status = ?, items = ?, error = ?, updated_at = ? WHERE kind = ? AND day = ?",
                 (status, items, error, now, kind, day))


def is_due(kind: str, now: int) -> bool:
    d = datetime.fromtimestamp(now, TZ)
    if kind == "daily":
        return (d.hour, d.minute) >= DAILY_AT
    return d.weekday() == WEEKLY_AT[0] and (d.hour, d.minute) >= WEEKLY_AT[1:]


def deliver(conn, kind: str, now: int | None = None, *, base_url: str | None = None, resume_hash: str | None = None,
            sender: Callable[[dict, Digest], None] | None = None, env=None, again: bool = False,
            check_time: bool = True) -> dict:
    """One attempt for one kind. Never raises. Result 'status':
    not_due | not_configured | already_done | nothing_to_send | sent | failed."""
    now = int(time.time()) if now is None else now
    day = local_day(now)
    if check_time and not is_due(kind, now):
        return {"kind": kind, "status": "not_due", "day": day}
    settings, missing = mail_settings(env)
    if settings is None:
        return {"kind": kind, "status": "not_configured", "day": day, "missing": missing}
    try:
        if again:
            conn.execute("DELETE FROM digest_log WHERE kind = ? AND day = ?", (kind, day))
        digest = build(kind, conn, now, settings["APP_BASE_URL"], resume_hash)
        if not claim(conn, kind, day, now):
            return {"kind": kind, "status": "already_done", "day": day}
        if digest is None:
            _finish(conn, kind, day, "skipped", 0, None, now)
            conn.commit()
            return {"kind": kind, "status": "nothing_to_send", "day": day}
        try:
            (sender or send_gmail)(settings, digest)
        except Exception as exc:   # keep the reason, never the password
            _finish(conn, kind, day, "failed", 0, f"{type(exc).__name__}: {str(exc)[:200]}", now)
            conn.commit()
            return {"kind": kind, "status": "failed", "day": day, "error": type(exc).__name__}
        if digest.reminded_ids:
            conn.executemany("UPDATE post_state SET reminders_sent = reminders_sent + 1 WHERE source_id = ?",
                             [(i,) for i in digest.reminded_ids])
        _finish(conn, kind, day, "sent", len(digest.reminded_ids), None, now)
        conn.commit()
        return {"kind": kind, "status": "sent", "day": day, "subject": digest.subject, "counts": digest.counts}
    except Exception as exc:
        conn.rollback()
        return {"kind": kind, "status": "failed", "day": day, "error": f"{type(exc).__name__}: {exc}"}


_warned: set[str] = set()


def run_scheduled(conn, now: int | None = None, **kw) -> list[dict]:
    """Called by the worker every few minutes: the daily email from 07:30 and the Sunday summary from 18:00."""
    out = []
    for kind in ("daily", "weekly"):
        res = deliver(conn, kind, now, **kw)
        if res["status"] == "not_configured":
            key = f"{res['day']}"
            if key not in _warned:   # one clear line per day, not one per pass
                _warned.add(key)
                print(f"[worker] digest not sent: missing {', '.join(res['missing'])} in .env", flush=True)
        elif res["status"] in ("sent", "failed", "nothing_to_send"):
            print(f"[worker] {kind} digest: {res['status']}" + (f" ({res['error']})" if res.get("error") else ""), flush=True)
        if res["status"] != "not_due":
            out.append(res)
    return out
