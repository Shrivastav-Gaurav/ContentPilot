"""Daily email and Sunday summary, by hand.

    python -m scripts.send_digest --dry-run                      # print the daily email, send nothing
    python -m scripts.send_digest --dry-run --kind weekly
    python -m scripts.send_digest --dry-run --db data\\copy.sqlite3 --html out.html
    python -m scripts.send_digest --send-now                     # send today's daily email now (once per day)
    python -m scripts.send_digest --send-now --again             # ignore today's "already sent" record

--dry-run works on a temporary copy of the database and never writes to it, to the real file, or to the
mail server. --send-now needs GMAIL_USER, GMAIL_APP_PASSWORD, DIGEST_TO and APP_BASE_URL in .env, and it
counts the listed posts as reminded, exactly like the scheduled send. No model is ever called.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime

from dotenv import load_dotenv

from contentpilot import config, db
from contentpilot.copilot import digest, service

DEFAULT_BASE_URL = "https://postpilot-vm.tail7aeda9.ts.net"


def _copy_db(src: str) -> str:
    """A private copy to read from (online backup when possible, so WAL contents come along)."""
    dst = os.path.join(tempfile.mkdtemp(prefix="cp_digest_"), "copy.sqlite3")
    try:
        a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        b = sqlite3.connect(dst)
        a.backup(b)
        a.close()
        b.close()
    except sqlite3.Error:
        shutil.copyfile(src, dst)
    return dst


def _resume_hash() -> str | None:
    try:
        return service.load_resume().hash
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print the email; send and write nothing")
    mode.add_argument("--send-now", action="store_true", help="send it now through Gmail")
    ap.add_argument("--kind", choices=("daily", "weekly"), default="daily")
    ap.add_argument("--db", help="database file to read (dry-run); default is the configured one")
    ap.add_argument("--html", help="dry-run: also save the HTML version to this file")
    ap.add_argument("--again", action="store_true", help="send-now: send even if this kind already went out today")
    ap.add_argument("--at", help="pretend it is this time, Asia/Kolkata, e.g. '2026-11-27 07:30'")
    args = ap.parse_args(argv)

    load_dotenv()
    now = int(time.time())
    if args.at:
        now = int(datetime.strptime(args.at, "%Y-%m-%d %H:%M").replace(tzinfo=digest.TZ).timestamp())

    if args.dry_run:
        src = args.db or config.DB_PATH
        if not os.path.exists(src):
            print(f"No database at {src}")
            return 1
        db._DB_PATH = _copy_db(src)
        conn = db.get_connection()
        try:
            base = os.environ.get("APP_BASE_URL", "").strip() or DEFAULT_BASE_URL
            d = digest.build(args.kind, conn, now, base, _resume_hash())
            if d is None:
                print(f"[dry run] Nothing due, no job to apply to, nothing to decide: no {args.kind} email would be sent.")
                return 0
            print(f"[dry run] nothing sent. Source: {src} (copied)\nSubject: {d.subject}\n" + "-" * 60)
            print(d.text)
            print("-" * 60 + f"\nWould count {len(d.reminded_ids)} post(s) as reminded.")
            if args.html:
                with open(args.html, "w", encoding="utf-8") as f:
                    f.write(d.html)
                print(f"HTML saved to {args.html}")
        finally:
            conn.close()
            shutil.rmtree(os.path.dirname(db._DB_PATH), ignore_errors=True)
        return 0

    conn = db.get_connection()
    try:
        res = digest.deliver(conn, args.kind, now, resume_hash=_resume_hash(), again=args.again, check_time=False)
    finally:
        conn.close()
    status = res["status"]
    if status == "not_configured":
        print("Not sent: missing " + ", ".join(res["missing"]) + " in .env")
        return 2
    msgs = {"sent": "Sent: " + str(res.get("subject", "")), "already_done": f"Already handled today ({res['day']}); use --again to send anyway.",
            "nothing_to_send": "Nothing due, no job to apply to, nothing to decide: no email sent.",
            "failed": "Send failed: " + str(res.get("error", ""))}
    print(msgs.get(status, status))
    return 0 if status in ("sent", "already_done", "nothing_to_send") else 1


if __name__ == "__main__":
    sys.exit(main())
