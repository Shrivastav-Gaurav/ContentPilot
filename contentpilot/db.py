import os
import sqlite3
import time

from contentpilot import config

_DB_PATH = config.DB_PATH

_SCHEMA_VERSION = 1

_MIGRATIONS = {
    1: """
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            source_type TEXT NOT NULL,
            external_id TEXT,
            canonical_url TEXT NOT NULL UNIQUE,
            title TEXT,
            author_id INTEGER REFERENCES authors(id),
            channel_id TEXT,
            published_at INTEGER,
            fetched_at INTEGER,
            language TEXT,
            duration_sec INTEGER,
            raw_text TEXT,
            content_hash TEXT,
            fetch_status TEXT,
            fetch_error TEXT
        );

        CREATE TABLE authors (
            id INTEGER PRIMARY KEY,
            name TEXT,
            youtube_channel_id TEXT,
            linkedin_url TEXT
        );

        CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            source_id INTEGER REFERENCES sources(id),
            lens TEXT,
            prompt_version TEXT,
            schema_version INTEGER,
            model TEXT,
            extraction_json TEXT,
            summary TEXT,
            status TEXT,
            error TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            extracted_at INTEGER,
            embedded_at INTEGER,
            UNIQUE(source_id, lens, prompt_version)
        );

        CREATE TABLE tags (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE
        );

        CREATE TABLE card_tags (
            card_id INTEGER,
            tag_id INTEGER,
            tag_source TEXT,
            PRIMARY KEY (card_id, tag_id)
        );

        CREATE TABLE ingest_jobs (
            id INTEGER PRIMARY KEY,
            job_type TEXT,
            target TEXT,
            status TEXT,
            attempts INTEGER,
            last_error TEXT,
            created_at INTEGER,
            finished_at INTEGER
        );
    """,
    2: """
        ALTER TABLE ingest_jobs ADD COLUMN total_items INTEGER DEFAULT 0;
        ALTER TABLE ingest_jobs ADD COLUMN done_items INTEGER DEFAULT 0;
        ALTER TABLE ingest_jobs ADD COLUMN failed_items INTEGER DEFAULT 0;
        ALTER TABLE ingest_jobs ADD COLUMN cost_usd REAL DEFAULT 0;
        ALTER TABLE ingest_jobs ADD COLUMN cost_cap_usd REAL;
        ALTER TABLE ingest_jobs ADD COLUMN started_at INTEGER;

        CREATE TABLE ingest_job_items (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES ingest_jobs(id),
            source_url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            card_id INTEGER REFERENCES cards(id),
            updated_at INTEGER
        );

        CREATE INDEX idx_ingest_job_items_job_id ON ingest_job_items(job_id);
        CREATE INDEX idx_ingest_job_items_status ON ingest_job_items(status);
    """,
    3: """
        ALTER TABLE sources ADD COLUMN saved_at INTEGER;

        CREATE TABLE card_flags (
            card_id INTEGER NOT NULL REFERENCES cards(id),
            flag TEXT NOT NULL CHECK (flag IN ('study', 'build', 'apply', 'share')),
            flagged_at INTEGER NOT NULL,
            PRIMARY KEY (card_id, flag)
        );
    """,
    4: """
        -- key_learnings_text holds the flattened JSON array (key_learnings)
        -- as space-joined text; populated by the app (not a generated column)
        -- since extraction_json's structure isn't directly FTS-indexable.
        CREATE VIRTUAL TABLE cards_fts USING fts5(
            card_id UNINDEXED,
            summary,
            key_learnings_text
        );
    """,
}


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at INTEGER
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row["version"] for row in rows}


def migrate() -> None:
    conn = _connect()
    try:
        _ensure_migrations_table(conn)
        applied = _applied_versions(conn)
        for version in sorted(_MIGRATIONS):
            if version in applied:
                continue
            conn.executescript(_MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, int(time.time())),
            )
        conn.commit()
    finally:
        conn.close()


def get_connection() -> sqlite3.Connection:
    migrate()
    return _connect()
