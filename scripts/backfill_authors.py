"""One-off backfill: extract author names from already-ingested LinkedIn
sources' raw_text (using the pattern added to fetchers/jina.py) and link them
to the authors table. Safe to re-run. Run with:
    python -m scripts.backfill_authors
"""
from dotenv import load_dotenv

from contentpilot.db import get_connection
from contentpilot.fetchers.jina import _extract_author, _extract_author_url


def main() -> None:
    load_dotenv()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, raw_text FROM sources
            WHERE source_type IN ('linkedin', 'article')
              AND author_id IS NULL
              AND raw_text IS NOT NULL
            """
        ).fetchall()

        updated = 0
        for row in rows:
            name = _extract_author(row["raw_text"])
            if not name:
                continue
            author_url = _extract_author_url(row["raw_text"])

            author_row = None
            if author_url:
                author_row = conn.execute(
                    "SELECT id FROM authors WHERE linkedin_url = ?", (author_url,)
                ).fetchone()
            if author_row is None:
                cursor = conn.execute(
                    "INSERT INTO authors (name, linkedin_url) VALUES (?, ?)",
                    (name, author_url),
                )
                author_id = cursor.lastrowid
            else:
                author_id = author_row["id"]

            conn.execute("UPDATE sources SET author_id = ? WHERE id = ?", (author_id, row["id"]))
            updated += 1

        conn.commit()
        print(f"Backfilled author for {updated}/{len(rows)} sources.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
