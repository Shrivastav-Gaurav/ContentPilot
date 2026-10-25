import hashlib
import json
import time

import chromadb

from contentpilot import config
from contentpilot.db import get_connection
from contentpilot.extraction import ExtractionResult
from contentpilot.fetchers.base import RawContent

_CHROMA_PATH = config.CHROMA_PATH
_COLLECTION_NAME = "insights_v1_minilm"

_chroma_client = None


def _get_chroma_collection():
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=_CHROMA_PATH)
    return _chroma_client.get_or_create_collection(
        _COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_source_by_url(canonical_url: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sources WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_card(source_id: int, lens: str, prompt_version: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM cards WHERE source_id = ? AND lens = ? AND prompt_version = ?",
            (source_id, lens, prompt_version),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_or_create_author(
    conn, name: str | None, channel_id: str | None, author_url: str | None = None
) -> int | None:
    if not name and not channel_id and not author_url:
        return None

    row = None
    if channel_id:
        row = conn.execute(
            "SELECT id FROM authors WHERE youtube_channel_id = ?", (channel_id,)
        ).fetchone()
    if row is None and author_url:
        row = conn.execute(
            "SELECT id FROM authors WHERE linkedin_url = ?", (author_url,)
        ).fetchone()
    if row:
        return row["id"]

    cursor = conn.execute(
        "INSERT INTO authors (name, youtube_channel_id, linkedin_url) VALUES (?, ?, ?)",
        (name, channel_id, author_url),
    )
    return cursor.lastrowid


def save_source(raw: RawContent) -> int:
    conn = get_connection()
    try:
        author_id = _get_or_create_author(conn, raw.author_name, raw.author_channel_id, raw.author_url)
        now = int(time.time())
        status = getattr(raw, "fetch_status", "ok")

        existing = conn.execute(
            "SELECT id FROM sources WHERE canonical_url = ?", (raw.canonical_url,)
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE sources SET
                    title = ?, author_id = ?, published_at = ?, fetched_at = ?,
                    language = ?, duration_sec = ?, raw_text = ?, content_hash = ?,
                    fetch_status = ?, fetch_error = NULL
                WHERE id = ?
                """,
                (
                    raw.title, author_id, raw.published_at, now,
                    raw.language, raw.duration_sec, raw.text, _content_hash(raw.text),
                    status, existing["id"],
                ),
            )
            conn.commit()
            return existing["id"]

        cursor = conn.execute(
            """
            INSERT INTO sources (
                source_type, external_id, canonical_url, title, author_id,
                published_at, fetched_at, language, duration_sec, raw_text,
                content_hash, fetch_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw.source_type, raw.external_id, raw.canonical_url, raw.title, author_id,
                raw.published_at, now, raw.language, raw.duration_sec, raw.text,
                _content_hash(raw.text), status,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def save_card(source_id: int, lens: str, result: ExtractionResult) -> int:
    conn = get_connection()
    try:
        extraction_json = result.extraction.model_dump_json()
        now = int(time.time())

        existing = conn.execute(
            "SELECT id FROM cards WHERE source_id = ? AND lens = ? AND prompt_version = ?",
            (source_id, lens, result.prompt_version),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE cards SET
                    schema_version = 1, model = ?, extraction_json = ?, summary = ?,
                    status = 'ok', error = NULL, tokens_in = ?, tokens_out = ?,
                    cost_usd = ?, extracted_at = ?
                WHERE id = ?
                """,
                (
                    result.model, extraction_json, result.extraction.summary,
                    result.tokens_in, result.tokens_out, result.cost_usd, now,
                    existing["id"],
                ),
            )
            conn.commit()
            card_id = existing["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO cards (
                    source_id, lens, prompt_version, schema_version, model,
                    extraction_json, summary, status, tokens_in, tokens_out,
                    cost_usd, extracted_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?, 'ok', ?, ?, ?, ?)
                """,
                (
                    source_id, lens, result.prompt_version, result.model,
                    extraction_json, result.extraction.summary,
                    result.tokens_in, result.tokens_out, result.cost_usd, now,
                ),
            )
            conn.commit()
            card_id = cursor.lastrowid

        _index_card(conn, card_id)
        return card_id
    finally:
        conn.close()


def _index_card(conn, card_id: int) -> None:
    row = conn.execute(
        """
        SELECT cards.*, sources.canonical_url, sources.title AS source_title,
               sources.source_type, sources.published_at, sources.author_id
        FROM cards JOIN sources ON cards.source_id = sources.id
        WHERE cards.id = ?
        """,
        (card_id,),
    ).fetchone()
    if row is None:
        return

    extraction = json.loads(row["extraction_json"])
    collection = _get_chroma_collection()

    ids, documents, metadatas = [], [], []

    def add(kind: str, index: int, text: str) -> None:
        ids.append(f"{card_id}:{kind}:{index}")
        documents.append(text)
        metadatas.append({
            "card_id": card_id,
            "source_id": row["source_id"],
            "source_type": row["source_type"],
            "canonical_url": row["canonical_url"],
            "kind": kind,
            "topics": ",".join(extraction.get("topics", [])),
            "published_at": row["published_at"] or 0,
            "prompt_version": row["prompt_version"],
        })

    add("summary", 0, extraction.get("summary", ""))
    for i, learning in enumerate(extraction.get("key_learnings", [])):
        add("learning", i, learning)
    for i, idea in enumerate(extraction.get("build_ideas", [])):
        add("idea", i, idea)
    for i, signal in enumerate(extraction.get("job_signals", [])):
        add("signal", i, signal)

    if ids:
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

    conn.execute("UPDATE cards SET embedded_at = ? WHERE id = ?", (int(time.time()), card_id))
    conn.commit()


def reconcile_index() -> int:
    """Index any cards that saved successfully but never got embedded (e.g. after a crash)."""
    conn = get_connection()
    try:
        card_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM cards WHERE status = 'ok' AND embedded_at IS NULL"
            )
        ]
        for card_id in card_ids:
            _index_card(conn, card_id)
        return len(card_ids)
    finally:
        conn.close()


def rebuild_index() -> int:
    """Re-embed all cards from SQLite into Chroma. SQLite is the source of truth."""
    conn = get_connection()
    try:
        global _chroma_client
        if _chroma_client is None:
            _chroma_client = chromadb.PersistentClient(path=_CHROMA_PATH)
        _chroma_client.delete_collection(_COLLECTION_NAME)

        card_ids = [row["id"] for row in conn.execute("SELECT id FROM cards WHERE status = 'ok'")]
        for card_id in card_ids:
            _index_card(conn, card_id)
        return len(card_ids)
    finally:
        conn.close()


def get_full_card(source_id: int, lens: str, prompt_version: str) -> dict | None:
    card = get_card(source_id, lens, prompt_version)
    if not card:
        return None
    return {**card, "extraction": json.loads(card["extraction_json"])}


# --- Ingest jobs ---------------------------------------------------------

def create_ingest_job(
    job_type: str, target: str, urls: list[str], cost_cap_usd: float
) -> int:
    conn = get_connection()
    try:
        now = int(time.time())
        cursor = conn.execute(
            """
            INSERT INTO ingest_jobs (
                job_type, target, status, attempts, total_items, done_items,
                failed_items, cost_usd, cost_cap_usd, created_at
            ) VALUES (?, ?, 'queued', 0, ?, 0, 0, 0, ?, ?)
            """,
            (job_type, target, len(urls), cost_cap_usd, now),
        )
        job_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO ingest_job_items (job_id, source_url, status, attempts, updated_at) "
            "VALUES (?, ?, 'queued', 0, ?)",
            [(job_id, url, now) for url in urls],
        )
        conn.commit()
        return job_id
    finally:
        conn.close()


def get_ingest_job(job_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM ingest_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_ingest_jobs(limit: int = 20) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_job_items(job_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_job_items WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def job_item_status_counts(job_id: int) -> dict[str, int]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM ingest_job_items WHERE job_id = ? GROUP BY status",
            (job_id,),
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}
    finally:
        conn.close()


def request_job_cancel(job_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE ingest_jobs SET status = 'cancelled' WHERE id = ? AND status IN ('queued', 'running')",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()


def retry_failed_items(job_id: int) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE ingest_job_items SET status = 'queued', error = NULL "
            "WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        if cursor.rowcount:
            conn.execute(
                "UPDATE ingest_jobs SET status = 'queued', failed_items = failed_items - ? "
                "WHERE id = ?",
                (cursor.rowcount, job_id),
            )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
