"""Hybrid retrieval over ingested cards: semantic search (ChromaDB, cosine)
merged with lexical search (SQLite FTS5), deduplicated by card_id.

Chroma is optional. When chromadb is not installed, or CONTENTPILOT_SEARCH=fts,
retrieve() searches posts_fts (saved posts) plus prep_notes (filed notes and your
own stories) with SQLite FTS5 and bm25 instead, returning the same RetrievedChunk
shape so the synthesizer works unchanged.
"""
import importlib.util
import os
import re
from dataclasses import dataclass

from contentpilot.copilot.parsing import body_text
from contentpilot.db import get_connection
from contentpilot.storage import _get_chroma_collection

DEFAULT_TOP_K = 8
WIDE_TOP_K = 15
MIN_CHUNKS_BEFORE_WIDENING = 3

_SEMANTIC_WEIGHT = 0.7
_FTS_WEIGHT = 0.3


@dataclass
class RetrievedChunk:
    card_id: int
    source_title: str | None
    author_name: str | None
    chunk_text: str
    distance: float  # 0 = perfect match, 1 = unrelated (cosine distance)
    url: str


def _card_display_info(conn, card_id: int) -> dict:
    row = conn.execute(
        """
        SELECT sources.title AS source_title, sources.canonical_url AS url,
               authors.name AS author_name
        FROM cards
        JOIN sources ON cards.source_id = sources.id
        LEFT JOIN authors ON sources.author_id = authors.id
        WHERE cards.id = ?
        """,
        (card_id,),
    ).fetchone()
    if row is None:
        return {"source_title": None, "author_name": None, "url": ""}
    return dict(row)


def _semantic_search(query_text: str, top_k: int, filter_topics: list[str] | None) -> list[dict]:
    collection = _get_chroma_collection()
    where = None
    if filter_topics:
        # topics metadata is a comma-joined string, not a list — Chroma can't
        # do substring filtering, so topic filtering is applied post-hoc below.
        where = None

    result = collection.query(
        query_texts=[query_text],
        n_results=top_k,
        where=where,
        include=["metadatas", "documents", "distances"],
    )

    hits = []
    ids = result.get("ids", [[]])[0]
    for i, chunk_id in enumerate(ids):
        metadata = result["metadatas"][0][i]
        if filter_topics:
            card_topics = (metadata.get("topics") or "").lower()
            if not any(t.lower() in card_topics for t in filter_topics):
                continue
        hits.append({
            "card_id": metadata["card_id"],
            "chunk_text": result["documents"][0][i],
            "distance": result["distances"][0][i],
        })
    return hits


def _fts_search(query_text: str, top_k: int) -> list[dict]:
    # FTS5 MATCH needs simple tokens; strip characters that break the query syntax.
    safe_terms = " ".join(
        f'"{term}"' for term in query_text.replace('"', " ").split() if term
    )
    if not safe_terms:
        return []

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT cards_fts.card_id AS card_id, cards_fts.rank AS rank,
                   cards.summary AS chunk_text
            FROM cards_fts
            JOIN cards ON cards.id = cards_fts.card_id
            WHERE cards_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (safe_terms, top_k),
        ).fetchall()
    except Exception:
        # Malformed FTS query (e.g. a lone operator-like token) — treat as no lexical hits.
        return []
    finally:
        conn.close()

    if not rows:
        return []

    # Normalize bm25 rank (more negative = better) to a 0..1 "distance"-like
    # scale so it can combine with cosine distance on the same axis.
    ranks = [r["rank"] for r in rows]
    worst, best = max(ranks), min(ranks)
    span = (worst - best) or 1.0

    return [
        {
            "card_id": r["card_id"],
            "chunk_text": r["chunk_text"],
            "fts_distance": (r["rank"] - best) / span,
        }
        for r in rows
    ]


# --- FTS-only path (no Chroma) ---------------------------------------------------

_WORD_RE = re.compile(r"[\w][\w'’-]*", re.UNICODE)
# Question words carry no topic signal; with OR-matching they would match everything.
_STOPWORDS = set("""a an and are as at be but by can could did do does for from had has have how i if in into
is it its me my not of on or our should so than that the their them then there these they this to
us was we were what when where which who why will with would you your tell about give example time get""".split())
_BODY_CHARS = 1500
# bm25 column weights for posts_fts(source_id, title, publisher, body, topics, notes):
# a publisher named "System Design Roadmap" should not outrank posts about roadmaps.
_POSTS_WEIGHTS = "0.0, 2.0, 0.3, 1.0, 1.5, 1.5"


def use_fts_only() -> bool:
    """True when CONTENTPILOT_SEARCH=fts or chromadb is not installed."""
    if os.environ.get("CONTENTPILOT_SEARCH", "").strip().lower() == "fts":
        return True
    return importlib.util.find_spec("chromadb") is None


def _or_query(text: str) -> str | None:
    """Any meaningful word may match (bm25 ranks posts that match more of them higher)."""
    words = [w for w in _WORD_RE.findall(text or "") if w.lower() not in _STOPWORDS and len(w) >= 2]
    if not words:
        return None
    return " OR ".join('"' + w.replace('"', "") + ('"*' if len(w) >= 4 else '"') for w in dict.fromkeys(words))


def _bm25_to_distance(scores: list[float]) -> list[float]:
    """bm25() is negative, more negative = better. Map to 0 (best) .. 1 (worst) like cosine distance."""
    if not scores:
        return []
    best, worst = min(scores), max(scores)
    span = (worst - best) or 1.0
    return [(sc - best) / span for sc in scores]


def retrieve_fts(query_text: str, top_k: int = DEFAULT_TOP_K) -> list[RetrievedChunk]:
    """Top posts and prep notes by bm25. card_id holds the source id for posts and
    -<note id> for your own stories (no card behind them)."""
    query = _or_query(query_text)
    if not query:
        return []
    conn = get_connection()
    try:
        hits = []
        for r in conn.execute(
            f"SELECT f.source_id, bm25(posts_fts, {_POSTS_WEIGHTS}) AS score, s.title, s.canonical_url, s.raw_text, "
            "a.name AS author, ps.summary FROM posts_fts f JOIN sources s ON s.id = f.source_id "
            "LEFT JOIN authors a ON a.id = s.author_id LEFT JOIN post_state ps ON ps.source_id = s.id "
            "WHERE posts_fts MATCH ? ORDER BY score LIMIT ?", (query, top_k),
        ):
            text = body_text(r["raw_text"])[:_BODY_CHARS]
            if r["summary"]:
                text = f"Summary: {r['summary']}\n\n{text}"
            hits.append({"score": r["score"], "card_id": int(r["source_id"]), "title": r["title"],
                         "author": r["author"], "url": r["canonical_url"] or "", "text": text})

        # prep_notes has no index of its own: build a throwaway one (it is small).
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.prep_fts USING fts5("
                     "note_id UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2')")
        conn.execute("DELETE FROM temp.prep_fts")
        conn.execute("INSERT INTO temp.prep_fts (note_id, text) SELECT n.id, t.name || ' ' || n.text "
                     "FROM prep_notes n JOIN prep_topics t ON t.id = n.topic_id")
        for r in conn.execute(
            "SELECT p.note_id, bm25(prep_fts) AS score, n.text, n.source_id, t.name AS topic, "
            "s.title, s.canonical_url, a.name AS author FROM temp.prep_fts p "
            "JOIN prep_notes n ON n.id = p.note_id JOIN prep_topics t ON t.id = n.topic_id "
            "LEFT JOIN sources s ON s.id = n.source_id LEFT JOIN authors a ON a.id = s.author_id "
            "WHERE prep_fts MATCH ? ORDER BY score LIMIT ?", (query, top_k),
        ):
            mine = r["source_id"] is None
            hits.append({
                "score": r["score"],
                "card_id": -int(r["note_id"]) if mine else int(r["source_id"]),
                "title": f"My story: {r['topic']}" if mine else f"Prep note ({r['topic']}): {r['title'] or ''}",
                "author": "You" if mine else r["author"],
                "url": "" if mine else (r["canonical_url"] or ""),
                "text": r["text"],
            })
    finally:
        conn.close()

    hits.sort(key=lambda h: h["score"])
    hits = hits[:top_k]
    return [
        RetrievedChunk(card_id=h["card_id"], source_title=h["title"], author_name=h["author"],
                       chunk_text=h["text"], distance=d, url=h["url"])
        for h, d in zip(hits, _bm25_to_distance([h["score"] for h in hits]))
    ]


def retrieve(
    query_text: str,
    top_k: int = DEFAULT_TOP_K,
    filter_topics: list[str] | None = None,
) -> list[RetrievedChunk]:
    if use_fts_only():
        return retrieve_fts(query_text, top_k)
    semantic_hits = _semantic_search(query_text, top_k, filter_topics)
    fts_hits = _fts_search(query_text, top_k)

    fts_by_card = {h["card_id"]: h for h in fts_hits}
    semantic_by_card = {h["card_id"]: h for h in semantic_hits}

    all_card_ids = set(semantic_by_card) | set(fts_by_card)
    merged = []
    for card_id in all_card_ids:
        semantic = semantic_by_card.get(card_id)
        fts = fts_by_card.get(card_id)

        semantic_distance = semantic["distance"] if semantic else 1.0
        fts_distance = fts["fts_distance"] if fts else 1.0
        combined_distance = semantic_distance * _SEMANTIC_WEIGHT + fts_distance * _FTS_WEIGHT

        chunk_text = semantic["chunk_text"] if semantic else fts["chunk_text"]
        merged.append({"card_id": card_id, "chunk_text": chunk_text, "distance": combined_distance})

    merged.sort(key=lambda h: h["distance"])
    merged = merged[:top_k]

    if len(merged) < MIN_CHUNKS_BEFORE_WIDENING and top_k < WIDE_TOP_K:
        return retrieve(query_text, top_k=WIDE_TOP_K, filter_topics=filter_topics)

    conn = get_connection()
    try:
        chunks = []
        for hit in merged:
            info = _card_display_info(conn, hit["card_id"])
            chunks.append(RetrievedChunk(
                card_id=hit["card_id"],
                source_title=info["source_title"],
                author_name=info["author_name"],
                chunk_text=hit["chunk_text"],
                distance=hit["distance"],
                url=info["url"],
            ))
        return chunks
    finally:
        conn.close()
