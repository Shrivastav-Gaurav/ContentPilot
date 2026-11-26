"""Remove the 6 fake test posts (https://li/1 .. /6) that an earlier test run wrote
into the real database. Backs up the database first. Safe to run more than once.

    python -m scripts.v3_cleanup_test_rows
"""
import shutil
import time

from contentpilot import config
from contentpilot.db import get_connection

FAKE_URLS = [f"https://li/{i}" for i in range(1, 7)]


def main() -> None:
    backup = f"{config.DB_PATH}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(config.DB_PATH, backup)
    print(f"Backup: {backup}")
    conn = get_connection()
    try:
        marks = ",".join("?" * len(FAKE_URLS))
        ids = [r["id"] for r in conn.execute(f"SELECT id FROM sources WHERE canonical_url IN ({marks})", FAKE_URLS)]
        print(f"Fake posts found: {len(ids)}")
        if ids:
            id_marks = ",".join("?" * len(ids))
            for table in ("post_state", "actions", "job_tracking", "prep_notes", "posts_fts"):
                conn.execute(f"DELETE FROM {table} WHERE source_id IN ({id_marks})", ids)
            conn.execute(f"DELETE FROM sources WHERE id IN ({id_marks})", ids)
            conn.commit()
        print(f"Real posts remaining: {conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
