import hashlib
import json
import time

from contentpilot import config
from contentpilot.copilot.spend import log_cost
from contentpilot.db import get_connection
from contentpilot.extraction import ExtractionResult
from contentpilot.fetchers.base import RawContent

_CHROMA_PATH = config.CHROMA_PATH
_COLLECTION_NAME = "insights_v1_minilm"

_chroma_client = None


def chroma_available() -> bool:
    """chromadb is optional (left out of requirements-vm.txt)."""
    import importlib.util

    return importlib.util.find_spec("chromadb") is not None


def _get_chroma_collection():
    global _chroma_client
    if _chroma_client is None:
        import chromadb  # optional: only the vector index needs it

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

        log_cost(conn, "extraction", f"card:{card_id}@{time.time_ns()}", result.model, result.cost_usd,
                 {"source_id": source_id, "lens": lens}, now=now, version=result.prompt_version)
        conn.commit()

        _index_card(conn, card_id)
        _sync_fts(conn, card_id)
        return card_id
    finally:
        conn.close()


def _sync_fts(conn, card_id: int) -> None:
    row = conn.execute("SELECT extraction_json, summary FROM cards WHERE id = ?", (card_id,)).fetchone()
    if row is None:
        return

    extraction = json.loads(row["extraction_json"])
    key_learnings_text = " ".join(extraction.get("key_learnings", []))

    conn.execute("DELETE FROM cards_fts WHERE card_id = ?", (card_id,))
    conn.execute(
        "INSERT INTO cards_fts (card_id, summary, key_learnings_text) VALUES (?, ?, ?)",
        (card_id, row["summary"] or "", key_learnings_text),
    )
    conn.commit()


def backfill_fts() -> int:
    """(Re)populates cards_fts for every ok card. Safe to re-run."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM cards_fts")
        card_ids = [row["id"] for row in conn.execute("SELECT id FROM cards WHERE status = 'ok'")]
        for card_id in card_ids:
            _sync_fts(conn, card_id)
        return len(card_ids)
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

    if not chroma_available():
        return  # no vector index on this machine (e.g. the VM); embedded_at stays NULL for later
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
        if not chroma_available():
            return 0
        _get_chroma_collection()  # opens the client
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


# --- Saved items / flags ---------------------------------------------------

VALID_FLAGS = ("study", "build", "apply", "share")


def mark_source_saved(source_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE sources SET saved_at = ? WHERE id = ? AND saved_at IS NULL",
            (int(time.time()), source_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_saved_cards(lens: str = "tpm") -> list[dict]:
    """Cards whose source was marked saved, newest-saved first, with their flags."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT cards.*, sources.canonical_url, sources.title AS source_title,
                   sources.saved_at, sources.source_type
            FROM cards
            JOIN sources ON cards.source_id = sources.id
            WHERE sources.saved_at IS NOT NULL AND cards.lens = ? AND cards.status = 'ok'
            ORDER BY sources.saved_at DESC
            """,
            (lens,),
        ).fetchall()

        cards = []
        for row in rows:
            card = dict(row)
            card["extraction"] = json.loads(card["extraction_json"])
            flag_rows = conn.execute(
                "SELECT flag FROM card_flags WHERE card_id = ?", (card["id"],)
            ).fetchall()
            card["flags"] = {r["flag"] for r in flag_rows}
            cards.append(card)
        return cards
    finally:
        conn.close()


def toggle_card_flag(card_id: int, flag: str) -> bool:
    """Toggles a flag on a card. Returns True if now set, False if now cleared."""
    if flag not in VALID_FLAGS:
        raise ValueError(f"Invalid flag: {flag!r}. Expected one of {VALID_FLAGS}.")

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT 1 FROM card_flags WHERE card_id = ? AND flag = ?", (card_id, flag)
        ).fetchone()

        if existing:
            conn.execute(
                "DELETE FROM card_flags WHERE card_id = ? AND flag = ?", (card_id, flag)
            )
            conn.commit()
            return False

        conn.execute(
            "INSERT INTO card_flags (card_id, flag, flagged_at) VALUES (?, ?, ?)",
            (card_id, flag, int(time.time())),
        )
        conn.commit()
        return True
    finally:
        conn.close()


# --- Card chat -------------------------------------------------------------

def save_chat_turn(card_id: int, role: str, content: str) -> None:
    if role not in ("user", "assistant"):
        raise ValueError(f"Invalid role: {role!r}. Expected 'user' or 'assistant'.")

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO card_chats (card_id, role, content) VALUES (?, ?, ?)",
            (card_id, role, content),
        )
        conn.commit()
    finally:
        conn.close()


def load_chat_history(card_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT role, content FROM card_chats WHERE card_id = ? ORDER BY created_at, id",
            (card_id,),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    finally:
        conn.close()


def clear_chat_history(card_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("DELETE FROM card_chats WHERE card_id = ?", (card_id,))
        conn.commit()
    finally:
        conn.close()


def get_card_chat_context(card_id: int) -> dict | None:
    """Everything card_chat_agent needs to build its system prompt: card
    extraction fields plus source title/author and the raw source text."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT cards.extraction_json, sources.title AS source_title,
                   sources.raw_text, authors.name AS author_name
            FROM cards
            JOIN sources ON cards.source_id = sources.id
            LEFT JOIN authors ON sources.author_id = authors.id
            WHERE cards.id = ?
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            return None

        extraction = json.loads(row["extraction_json"])
        return {
            "title": extraction.get("title") or row["source_title"] or "Untitled",
            "author": row["author_name"] or "Unknown",
            "summary": extraction.get("summary", ""),
            "key_learnings": extraction.get("key_learnings", []),
            "topics": extraction.get("topics", []),
            "build_ideas": extraction.get("build_ideas", []),
            "job_signals": extraction.get("job_signals", []),
            "content_snippet": (row["raw_text"] or "")[:3000],
        }
    finally:
        conn.close()
