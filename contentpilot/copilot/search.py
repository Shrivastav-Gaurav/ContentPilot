"""Search every post ever saved: full text, title, publisher, topics, your notes.

Plain SQLite FTS5, no API calls. Status never hides a post from search.
"""
import json
import re

from contentpilot.copilot.parsing import body_text, display_title, parse_title

_TOKEN_RE = re.compile(r"[\w][\w'’-]*", re.UNICODE)


def _notes_for(conn, source_id: int) -> str:
    rows = conn.execute(
        "SELECT note AS t FROM actions WHERE source_id = ? AND note IS NOT NULL "
        "UNION ALL SELECT note FROM job_tracking WHERE source_id = ? AND note IS NOT NULL "
        "UNION ALL SELECT text FROM prep_notes WHERE source_id = ?",
        (source_id, source_id, source_id),
    ).fetchall()
    return " ".join(r["t"] for r in rows)


def _topics_for(conn, source_id: int) -> str:
    topics: list[str] = []
    for row in conn.execute(
        "SELECT extraction_json FROM cards WHERE source_id = ? AND extraction_json IS NOT NULL", (source_id,)
    ):
        try:
            topics += json.loads(row["extraction_json"]).get("topics", [])
        except (ValueError, TypeError):
            pass
    for row in conn.execute(
        "SELECT o.content_json FROM outputs o WHERE o.kind = 'triage' AND o.cache_key LIKE ?", (f"{source_id}:%",)
    ):
        try:
            topics += json.loads(row["content_json"]).get("topics", [])
        except (ValueError, TypeError):
            pass
    return " ".join(dict.fromkeys(topics))


def index_source(conn, source_id: int) -> None:
    row = conn.execute(
        "SELECT s.id, s.title, s.raw_text, a.name AS publisher FROM sources s "
        "LEFT JOIN authors a ON a.id = s.author_id WHERE s.id = ?",
        (source_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM posts_fts WHERE source_id = ?", (source_id,))
    conn.execute(
        "INSERT INTO posts_fts (source_id, title, publisher, body, topics, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (
            source_id,
            row["title"] or parse_title(row["raw_text"]) or "",
            row["publisher"] or "",
            body_text(row["raw_text"]),
            _topics_for(conn, source_id),
            _notes_for(conn, source_id),
        ),
    )


def rebuild_index(conn) -> int:
    conn.execute("DELETE FROM posts_fts")
    ids = [r["id"] for r in conn.execute("SELECT id FROM sources")]
    for source_id in ids:
        index_source(conn, source_id)
    return len(ids)


_PHRASE_RE = re.compile(r'"([^"]*)"')


def _fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query.

    Text in double quotes is an exact phrase ("neural network"); every other word
    must match too (prefix match for words of 4+ letters). An unpaired quote is ignored.
    """
    text = text or ""
    parts = []
    for phrase in _PHRASE_RE.findall(text):
        words = _TOKEN_RE.findall(phrase)
        if words:
            parts.append('"' + " ".join(w.replace('"', "") for w in words) + '"')
    # Prefix match only for words of 4+ letters, so "RAG" does not match "ragged".
    for t in _TOKEN_RE.findall(_PHRASE_RE.sub(" ", text)):
        parts.append('"' + t.replace('"', "") + ('"*' if len(t) >= 4 else '"'))
    return " AND ".join(parts) or None


def search(
    conn,
    text: str = "",
    status: str | None = None,
    post_type: str | None = None,
    published_since: int | None = None,
    source_type: str | None = None,
    limit: int = 50,
    by_meaning: bool = False,
    embedder=None,
    now: int | None = None,
    min_match: float | None = None,
) -> list[dict]:
    """Search all posts. status: None/'any', 'open', 'actioned' (done/read/parked), 'archived', 'dismissed'.

    by_meaning=True ranks posts by cosine similarity of stored embeddings to the text
    (filters still apply) and adds a 'similarity' to each row. Only posts at or above
    min_match (default: the search.min_match setting, 0.35) are returned. It embeds new posts and the
    query first (cost logged in outputs) and raises DailyCapReached instead of calling
    when the cap is reached. Leaves commit to the caller.
    """
    where, params = [], []
    ranked: dict[int, tuple[int, float]] = {}
    if by_meaning and (text or "").strip():
        from contentpilot.copilot import embeddings
        embeddings.embed_pending(conn, embedder=embedder, now=now)
        qvec = embeddings.embed_query(conn, text, embedder=embedder, now=now)
        hits = embeddings.rank(conn, "post", qvec, top_k=1000)
        threshold = get_min_match(conn) if min_match is None else min_match
        ranked = {item_id: (i, sim) for i, (item_id, sim) in enumerate(hits) if sim >= threshold}
        if not ranked:
            return []
        where.append(f"s.id IN ({','.join('?' * len(ranked))})")
        params += list(ranked)
    else:
        query = _fts_query(text)
        if query:
            where.append("s.id IN (SELECT source_id FROM posts_fts WHERE posts_fts MATCH ?)")
            params.append(query)
    if status and status != "any":
        group = {"actioned": ("done", "read", "parked")}.get(status, (status,))
        where.append(f"COALESCE(ps.status, 'open') IN ({','.join('?' * len(group))})")
        params += list(group)
    if post_type:
        where.append("ps.post_type = ?")
        params.append(post_type)
    if published_since:
        where.append("s.published_at >= ?")
        params.append(published_since)
    if source_type:
        where.append("s.source_type = ?")
        params.append(source_type)

    sql = (
        "SELECT s.id, s.canonical_url, s.source_type, s.published_at, "
        "COALESCE(s.title, '') AS title, a.name AS publisher, "
        "ps.post_type, COALESCE(ps.status, 'open') AS status, ps.summary "
        "FROM sources s LEFT JOIN authors a ON a.id = s.author_id "
        "LEFT JOIN post_state ps ON ps.source_id = s.id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY COALESCE(s.published_at, s.fetched_at) DESC"
        + ("" if ranked else " LIMIT ?")
    )
    if not ranked:
        params.append(limit)
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["display_title"] = display_title(d["title"], d["summary"], d["publisher"])
        if ranked:
            d["similarity"] = ranked[d["id"]][1]
        out.append(d)
    if ranked:
        out.sort(key=lambda d: ranked[d["id"]][0])
        out = out[:limit]
    return out


MIN_MATCH_KEY = "search.min_match"
DEFAULT_MIN_MATCH = 0.35


def get_min_match(conn) -> float:
    """Lowest similarity (0-1) Search by meaning shows. Setting search.min_match; Ask ignores it."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (MIN_MATCH_KEY,)).fetchone()
    try:
        value = float(row[0]) if row else DEFAULT_MIN_MATCH
    except (TypeError, ValueError):
        return DEFAULT_MIN_MATCH
    return value / 100 if value > 1 else value  # accept "35" as well as "0.35"


def set_min_match(conn, value: float) -> None:
    value = value / 100 if value > 1 else value
    if not 0 <= value <= 1:
        raise ValueError("search.min_match must be between 0 and 1 (or 0-100)")
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (MIN_MATCH_KEY, str(value)))


def meaning_search_ready(conn) -> bool:
    """Search by meaning needs stored vectors and an OPENAI_API_KEY for the query."""
    from contentpilot.copilot import embeddings
    return embeddings.enabled() and embeddings.has_embeddings(conn)


def source_types(conn) -> list[str]:
    """Distinct source types (linkedin, youtube, article...) for the Search filter."""
    return [r["source_type"] for r in conn.execute(
        "SELECT DISTINCT source_type FROM sources WHERE source_type IS NOT NULL ORDER BY source_type")]
