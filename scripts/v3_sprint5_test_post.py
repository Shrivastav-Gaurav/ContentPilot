"""One throw-away post at 2 ignored reminders, to try Keep / Dismiss on a phone.

    python -m scripts.v3_sprint5_test_post --create    # make it (or reset it to 2 reminders, open, overdue)
    python -m scripts.v3_sprint5_test_post --remove    # delete it and everything about it

Back the database up first (deploy/README-vm.md, section 8). No model is called.
"""
import argparse
import time

from contentpilot.copilot import search
from contentpilot.copilot.rules import DAY
from contentpilot.db import get_connection

URL = "https://contentpilot.invalid/sprint5-keep-or-dismiss-test"
TITLE = "TEST: keep or dismiss me (Sprint 5 test post)"


def _id(conn):
    row = conn.execute("SELECT id FROM sources WHERE canonical_url = ?", (URL,)).fetchone()
    return row["id"] if row else None


def create(conn) -> int:
    now = int(time.time())
    sid = _id(conn)
    if sid is None:
        sid = conn.execute(
            "INSERT INTO sources (source_type, canonical_url, title, raw_text, fetched_at, saved_at, published_at) "
            "VALUES ('linkedin', ?, ?, 'Throw-away post for trying Keep and Dismiss.', ?, ?, ?)",
            (URL, TITLE, now, now - 10 * DAY, now - 10 * DAY)).lastrowid
    conn.execute(
        "INSERT OR REPLACE INTO post_state (source_id, post_type, status, summary, suggested_action, due_at, "
        "reminders_sent, updated_at) VALUES (?, 'learning', 'open', ?, 'Try the Keep and Dismiss buttons', ?, 2, ?)",
        (sid, TITLE, now - 2 * DAY, now))
    search.index_source(conn, sid)
    return sid


def remove(conn) -> bool:
    sid = _id(conn)
    if sid is None:
        return False
    for table in ("post_state", "actions", "job_tracking", "prep_notes", "posts_fts"):
        conn.execute(f"DELETE FROM {table} WHERE source_id = ?", (sid,))
    conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--create", action="store_true")
    g.add_argument("--remove", action="store_true")
    args = ap.parse_args()
    conn = get_connection()
    try:
        if args.create:
            print(f"Test post {create(conn)} is open at 2 ignored reminders: see 'Keep or dismiss?' in the Inbox.")
        else:
            print("Removed." if remove(conn) else "No test post found.")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
