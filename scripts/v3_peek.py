"""Peek at triage results: 5 random posts per post_type from post_state.

    python -m scripts.v3_peek
    python -m scripts.v3_peek --n 3

Read only.
"""
import argparse
import textwrap

from contentpilot.copilot.parsing import display_title
from contentpilot.db import get_connection


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=5, help="posts per post_type")
    args = parser.parse_args()

    conn = get_connection()
    try:
        types = [
            r[0]
            for r in conn.execute(
                "SELECT COALESCE(post_type, '(untriaged)') FROM post_state GROUP BY 1 ORDER BY 1"
            )
        ]
        if not types:
            print("post_state is empty. Run: python -m scripts.v3_backfill")
            return

        for post_type in types:
            total = conn.execute(
                "SELECT COUNT(*) FROM post_state WHERE COALESCE(post_type, '(untriaged)') = ?",
                (post_type,),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT a.name AS publisher, s.title, ps.summary, ps.suggested_action "
                "FROM post_state ps "
                "JOIN sources s ON s.id = ps.source_id "
                "LEFT JOIN authors a ON a.id = s.author_id "
                "WHERE COALESCE(ps.post_type, '(untriaged)') = ? "
                "ORDER BY RANDOM() LIMIT ?",
                (post_type, args.n),
            ).fetchall()

            print(f"\n=== {post_type} ({len(rows)} of {total}) ===")
            for i, r in enumerate(rows, 1):
                print(f"\n[{i}] {display_title(r[1], r[2], r[0])}")
                print(f"    publisher: {r[0] or '(unknown)'}")
                print(textwrap.fill(f"summary: {r[2] or '-'}", width=100,
                                    initial_indent="    ", subsequent_indent="      "))
                print(textwrap.fill(f"action:  {r[3] or '-'}", width=100,
                                    initial_indent="    ", subsequent_indent="      "))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
