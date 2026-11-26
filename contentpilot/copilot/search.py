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


def _fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word must match (prefix)."""
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return None
    # Prefix match only for words of 4+ letters, so "RAG" does not match "ragged".
    return " AND ".join('"' + t.replace('"', "") + ('"*' if len(t) >= 4 else '"') for t in tokens)


def search(
    conn,
    text: str = "",
    status: str | None = None,
    post_type: str | None = None,
    published_since: int | None = None,
    source_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search all posts. status: None/'any', 'open', 'actioned' (done/read/parked), 'archived', 'dismissed'."""
    where, params = [], []
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
        + " ORDER BY COALESCE(s.published_at, s.fetched_at) DESC LIMIT ?"
    )
    params.append(limit)
    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["display_title"] = display_title(d["title"], d["summary"], d["publisher"])
        out.append(d)
    return out


def source_types(conn) -> list[str]:
    """Distinct source types (linkedin, youtube, article...) for the Search filter."""
    return [r["source_type"] for r in conn.execute(
        "SELECT DISTINCT source_type FROM sources WHERE source_type IS NOT NULL ORDER BY source_type")]
