"""ContentPilot v3, Sprint 1 backfill.

    python -m scripts.v3_backfill --dry-run     # show what would happen, no API calls, no writes
    python -m scripts.v3_backfill               # apply: facts, triage (cached), rules, search index

Safe to run again: cached posts cost nothing and nothing is ever deleted.
"""
import argparse
import shutil
import time

from dotenv import load_dotenv

from contentpilot import config
from contentpilot.copilot import search, service
from contentpilot.db import get_connection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="roll back at the end; skip the LLM")
    parser.add_argument("--cap", type=float, default=None, help="daily API spend cap in USD (default: the api.daily_cap_usd setting, 0.25)")
    args = parser.parse_args()
    load_dotenv()

    if not args.dry_run:
        backup = f"{config.DB_PATH}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(config.DB_PATH, backup)
        print(f"Backup: {backup}")

    conn = get_connection()  # applies migration 6
    try:
        print("Facts from text:", service.backfill_facts(conn))

        if args.dry_run:
            pending = len(service._untriaged(conn))
            print(f"Would triage {pending} posts in ~{-(-pending // 10)} API calls (skipped in dry run).")
        else:
            print("Triage:", service.run_triage(conn, daily_cap_usd=args.cap))
            print("Daily rules:", service.apply_daily_rules(conn))

        print("Search index rows:", search.rebuild_index(conn))
        print("Status counts:", service.status_counts(conn))

        if args.dry_run:
            conn.rollback()
            print("Dry run: rolled back.")
        else:
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
