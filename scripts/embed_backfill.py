"""Embed every saved post and prep note for meaning-based search (no Chroma needed).

    python -m scripts.embed_backfill --dry-run   # what would be embedded and the estimated cost; no API calls
    python -m scripts.embed_backfill             # backs up the DB, embeds, prints the cost it spent

Uses OpenAI text-embedding-3-small (OPENAI_API_KEY in .env). Safe to run again:
unchanged posts and notes are skipped, and the cost is logged in outputs so the
daily cap counts it.
"""
import argparse
import shutil
import time

from dotenv import load_dotenv

from contentpilot import config
from contentpilot.copilot import embeddings
from contentpilot.copilot.spend import get_daily_cap, spent_today
from contentpilot.db import get_connection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="estimate only; no API calls, no writes")
    parser.add_argument("--cap", type=float, default=None, help="daily API spend cap in USD (all features) (default: the api.daily_cap_usd setting, 0.25)")
    args = parser.parse_args()
    load_dotenv()

    conn = get_connection()  # applies migration 7 (embeddings table)
    try:
        est = embeddings.estimate(conn)
        print(f"Model: {embeddings.MODEL}")
        print(f"To embed: {est['posts']} posts, {est['prep_notes']} prep notes "
              f"(~{est['est_tokens']:,} tokens, ~${est['est_cost_usd']:.5f})")
        print(f"Already embedded: {embeddings.counts(conn)}")
        if args.dry_run:
            return
        if not embeddings.enabled():
            raise SystemExit("OPENAI_API_KEY is not set in .env; nothing embedded.")
        if est["posts"] + est["prep_notes"] == 0:
            print("Nothing to do.")
            return

        backup = f"{config.DB_PATH}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(config.DB_PATH, backup)
        print(f"Backup: {backup}")

        before = spent_today(conn)
        stats = embeddings.embed_pending(conn, daily_cap_usd=args.cap)
        conn.commit()
        print(f"Embedded {stats['embedded']} of {stats['pending']} in {stats['calls']} call(s): "
              f"{stats['tokens']:,} tokens, cost ${stats['cost_usd']:.5f}")
        if stats["stopped_by_cap"]:
            print(f"Stopped at the ${get_daily_cap(conn) if args.cap is None else args.cap:.2f} daily cap; "
                  "run again tomorrow for the rest.")
        print(f"Spent today (all features): ${before:.4f} -> ${spent_today(conn):.4f}")
        print(f"Now embedded: {embeddings.counts(conn)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
