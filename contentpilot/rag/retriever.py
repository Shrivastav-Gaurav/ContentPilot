"""Hybrid retrieval over ingested cards: semantic search (ChromaDB, cosine)
merged with lexical search (SQLite FTS5), deduplicated by card_id.
"""
from dataclasses import dataclass

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


def retrieve(
    query_text: str,
    top_k: int = DEFAULT_TOP_K,
    filter_topics: list[str] | None = None,
) -> list[RetrievedChunk]:
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
