"""Meaning-based search without Chroma: OpenAI text-embedding-3-small vectors stored in
SQLite (table `embeddings`) and compared in memory with numpy. Works anywhere SQLite does.

- One vector per post (display title + summary + first 1,500 chars of body) and per prep
  note (topic + note text). Re-embedded only when that text changes (text_hash).
- Every API call's cost is written to `outputs` (kind 'embedding' / 'query_embedding'),
  so spent_today() and the daily cap include it.
- Query vectors are cached in outputs too: the same search twice costs one call.
"""
import base64
import hashlib
import json
import time
from typing import Callable

import numpy as np
import requests

from contentpilot import config
from contentpilot.copilot.parsing import body_text, display_title
from contentpilot.copilot.spend import check_cap

MODEL = config.EMBEDDING_MODEL
PRICE_PER_MTOK = 0.02          # USD per 1M input tokens for text-embedding-3-small
BATCH_SIZE = 64
BODY_CHARS = 1500
COST_KIND = "embedding"
QUERY_KIND = "query_embedding"

# embedder(texts) -> (vectors, total_tokens). Swappable for tests.
Embedder = Callable[[list[str]], tuple[list[list[float]], int]]


def openai_embedder(texts: list[str]) -> tuple[list[list[float]], int]:
    key = config.openai_api_key()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    response = requests.post(
        config.OPENAI_EMBEDDINGS_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "input": texts},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    data = sorted(payload["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data], int(payload.get("usage", {}).get("total_tokens") or 0)


def enabled() -> bool:
    """Embeddings can be made (a key is configured)."""
    return config.openai_api_key() is not None


def cost_of(tokens: int) -> float:
    return tokens * PRICE_PER_MTOK / 1_000_000


def text_hash(text: str) -> str:
    return hashlib.sha256(f"{MODEL}\n{text}".encode("utf-8")).hexdigest()[:16]


def post_input(title: str | None, summary: str | None, publisher: str | None, raw_text: str | None) -> str:
    head = display_title(title, summary, publisher)
    parts = [head]
    if summary and summary.strip() != head:
        parts.append(summary.strip())
    parts.append(body_text(raw_text)[:BODY_CHARS])
    return "\n\n".join(p for p in parts if p).strip()


def note_input(topic: str, text: str) -> str:
    return f"{topic}\n\n{text}".strip()


def _to_blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# --- what needs embedding ------------------------------------------------------------

def _wanted(conn) -> list[tuple[str, int, str]]:
    """(kind, item_id, input text) for every post with text and every prep note."""
    out = []
    for r in conn.execute(
        "SELECT s.id, s.title, s.raw_text, a.name AS publisher, ps.summary FROM sources s "
        "LEFT JOIN authors a ON a.id = s.author_id LEFT JOIN post_state ps ON ps.source_id = s.id "
        "WHERE s.raw_text IS NOT NULL AND TRIM(s.raw_text) != '' ORDER BY s.id"
    ):
        out.append(("post", r[0], post_input(r[1], r[4], r[3], r[2])))
    for r in conn.execute(
        "SELECT n.id, n.text, t.name FROM prep_notes n JOIN prep_topics t ON t.id = n.topic_id ORDER BY n.id"
    ):
        out.append(("prep_note", r[0], note_input(r[2], r[1])))
    return out


def pending(conn) -> list[tuple[str, int, str, str]]:
    """Items whose text is new or changed since they were embedded: (kind, id, text, hash)."""
    have = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT kind, item_id, text_hash FROM embeddings WHERE model = ?", (MODEL,))}
    todo = []
    for kind, item_id, text in _wanted(conn):
        h = text_hash(text)
        if have.get((kind, item_id)) != h:
            todo.append((kind, item_id, text, h))
    return todo


def estimate(conn) -> dict:
    """Dry run: how much a backfill would embed and roughly cost (~4 chars per token)."""
    todo = pending(conn)
    tokens = sum(len(t[2]) for t in todo) // 4
    return {"posts": sum(1 for t in todo if t[0] == "post"), "prep_notes": sum(1 for t in todo if t[0] == "prep_note"),
            "est_tokens": tokens, "est_cost_usd": cost_of(tokens)}


def counts(conn) -> dict:
    c = {"post": 0, "prep_note": 0}
    for r in conn.execute("SELECT kind, COUNT(*) FROM embeddings WHERE model = ? GROUP BY kind", (MODEL,)):
        c[r[0]] = r[1]
    return c


def has_embeddings(conn) -> bool:
    return conn.execute("SELECT 1 FROM embeddings WHERE model = ? LIMIT 1", (MODEL,)).fetchone() is not None


def _log_cost(conn, kind: str, key: str, content: dict, tokens: int, now: int) -> float:
    cost = cost_of(tokens)
    conn.execute(
        "INSERT OR REPLACE INTO outputs (kind, cache_key, prompt_version, content_json, model, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, key, MODEL, json.dumps({**content, "tokens": tokens}), MODEL, cost, now),
    )
    return cost


def embed_pending(conn, embedder: Embedder | None = None, now: int | None = None,
                  daily_cap_usd: float | None = None, batch_size: int = BATCH_SIZE) -> dict:
    """Embed every new or changed post and prep note. Unchanged items cost nothing.
    Stops cleanly at the daily cap. Leaves commit to the caller."""
    embedder = embedder or openai_embedder
    now = int(time.time()) if now is None else now
    todo = pending(conn)
    stats = {"pending": len(todo), "embedded": 0, "calls": 0, "tokens": 0, "cost_usd": 0.0,
             "stopped_by_cap": False, "removed": 0}
    for i in range(0, len(todo), batch_size):
        try:
            check_cap(conn, daily_cap_usd, now)
        except Exception:
            stats["stopped_by_cap"] = True
            break
        chunk = todo[i:i + batch_size]
        vectors, tokens = embedder([t[2] for t in chunk])
        if len(vectors) != len(chunk):
            raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(chunk)} inputs")
        for (kind, item_id, _text, h), vec in zip(chunk, vectors):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (kind, item_id, model, dim, vector, text_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (kind, item_id, MODEL, len(vec), _to_blob(vec), h, now))
        stats["calls"] += 1
        stats["tokens"] += tokens
        stats["cost_usd"] += _log_cost(conn, COST_KIND, f"{now}:{i}:{chunk[0][0]}:{chunk[0][1]}",
                                       {"count": len(chunk)}, tokens, now)
        stats["embedded"] += len(chunk)
    # Notes merged away by "Change topic" leave orphan vectors behind.
    stats["removed"] = conn.execute(
        "DELETE FROM embeddings WHERE kind = 'prep_note' AND item_id NOT IN (SELECT id FROM prep_notes)").rowcount
    return stats


def embed_query(conn, text: str, embedder: Embedder | None = None, now: int | None = None,
                daily_cap_usd: float | None = None) -> np.ndarray:
    """Vector for a search or question, cached in outputs by its normalised text.
    Raises DailyCapReached before a new call when the cap is reached."""
    norm = " ".join((text or "").split()).lower()
    key = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]
    row = conn.execute("SELECT content_json FROM outputs WHERE kind = ? AND cache_key = ? AND prompt_version = ?",
                       (QUERY_KIND, key, MODEL)).fetchone()
    if row:
        return np.frombuffer(base64.b64decode(json.loads(row[0])["vector"]), dtype=np.float32)
    now = int(time.time()) if now is None else now
    check_cap(conn, daily_cap_usd, now)
    vectors, tokens = (embedder or openai_embedder)([norm])
    vec = np.asarray(vectors[0], dtype=np.float32)
    _log_cost(conn, QUERY_KIND, key, {"vector": base64.b64encode(vec.tobytes()).decode("ascii")}, tokens, now)
    return vec


# --- similarity ----------------------------------------------------------------------------

_matrix_cache: dict = {}


def _matrix(conn, kind: str) -> tuple[list[int], np.ndarray | None]:
    """All vectors of a kind as a row-normalised matrix, cached until the table changes."""
    sig = conn.execute("SELECT COUNT(*), MAX(created_at), SUM(item_id) FROM embeddings WHERE kind = ? AND model = ?",
                       (kind, MODEL)).fetchone()
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    key = (kind, db_file, tuple(sig))
    hit = _matrix_cache.get(kind)
    if hit and hit[0] == key:
        return hit[1], hit[2]
    rows = conn.execute("SELECT item_id, vector FROM embeddings WHERE kind = ? AND model = ? ORDER BY item_id",
                        (kind, MODEL)).fetchall()
    if not rows:
        _matrix_cache[kind] = (key, [], None)
        return [], None
    ids = [r[0] for r in rows]
    m = np.vstack([_from_blob(r[1]) for r in rows])
    m = m / np.clip(np.linalg.norm(m, axis=1, keepdims=True), 1e-12, None)
    _matrix_cache[kind] = (key, ids, m)
    return ids, m


def rank(conn, kind: str, query_vec: np.ndarray, top_k: int = 20) -> list[tuple[int, float]]:
    """(item_id, cosine similarity), best first."""
    ids, m = _matrix(conn, kind)
    if m is None or not len(ids):
        return []
    q = np.asarray(query_vec, dtype=np.float32)
    if q.shape[0] != m.shape[1]:
        return []  # vectors from another model or dimension
    q = q / max(float(np.linalg.norm(q)), 1e-12)
    sims = m @ q
    order = np.argsort(-sims)[:top_k]
    return [(ids[i], float(sims[i])) for i in order]
