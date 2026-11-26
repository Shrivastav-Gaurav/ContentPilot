"""Retrieval for Ask (Prep Notebook answers).

Default (no Chroma): hybrid retrieval. Stored OpenAI embeddings (cosine, numpy) plus
SQLite FTS5 bm25 over saved posts and prep notes, merged by reciprocal rank fusion.
With no stored vectors, no OPENAI_API_KEY, or CONTENTPILOT_SEARCH=fts, it is FTS only.
CONTENTPILOT_SEARCH=chroma keeps the original Chroma + cards_fts path. Every path
returns the same RetrievedChunk shape, so the synthesizer works unchanged.
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
    """True unless the legacy Chroma path is chosen (CONTENTPILOT_SEARCH=chroma) and installed."""
    if os.environ.get("CONTENTPILOT_SEARCH", "").strip().lower() != "chroma":
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


def _fts_posts(conn, query: str, k: int) -> list[tuple[int, float]]:
    return [(int(r[0]), r[1]) for r in conn.execute(
        f"SELECT source_id, bm25(posts_fts, {_POSTS_WEIGHTS}) AS score FROM posts_fts "
        "WHERE posts_fts MATCH ? ORDER BY score LIMIT ?", (query, k))]


def _fts_notes(conn, query: str, k: int) -> list[tuple[int, float]]:
    # prep_notes has no index of its own: build a throwaway one (it is small).
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.prep_fts USING fts5("
                 "note_id UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2')")
    conn.execute("DELETE FROM temp.prep_fts")
    conn.execute("INSERT INTO temp.prep_fts (note_id, text) SELECT n.id, t.name || ' ' || n.text "
                 "FROM prep_notes n JOIN prep_topics t ON t.id = n.topic_id")
    return [(int(r[0]), r[1]) for r in conn.execute(
        "SELECT note_id, bm25(prep_fts) AS score FROM temp.prep_fts WHERE prep_fts MATCH ? ORDER BY score LIMIT ?",
        (query, k))]


def _post_hit(conn, source_id: int) -> dict | None:
    r = conn.execute(
        "SELECT s.id, s.title, s.canonical_url, s.raw_text, a.name AS author, ps.summary FROM sources s "
        "LEFT JOIN authors a ON a.id = s.author_id LEFT JOIN post_state ps ON ps.source_id = s.id WHERE s.id = ?",
        (source_id,)).fetchone()
    if r is None:
        return None
    text = body_text(r["raw_text"])[:_BODY_CHARS]
    if r["summary"]:
        text = f"Summary: {r['summary']}\n\n{text}"
    return {"card_id": int(r["id"]), "title": r["title"], "author": r["author"],
            "url": r["canonical_url"] or "", "text": text}


def _note_hit(conn, note_id: int) -> dict | None:
    r = conn.execute(
        "SELECT n.id, n.text, n.source_id, t.name AS topic, s.title, s.canonical_url, a.name AS author "
        "FROM prep_notes n JOIN prep_topics t ON t.id = n.topic_id LEFT JOIN sources s ON s.id = n.source_id "
        "LEFT JOIN authors a ON a.id = s.author_id WHERE n.id = ?", (note_id,)).fetchone()
    if r is None:
        return None
    mine = r["source_id"] is None
    return {
        "card_id": -int(r["id"]) if mine else int(r["source_id"]),
        "title": f"My story: {r['topic']}" if mine else f"Prep note ({r['topic']}): {r['title'] or ''}",
        "author": "You" if mine else r["author"],
        "url": "" if mine else (r["canonical_url"] or ""),
        "text": r["text"],
    }


def _chunks(hits: list[dict], distances: list[float]) -> list[RetrievedChunk]:
    return [RetrievedChunk(card_id=h["card_id"], source_title=h["title"], author_name=h["author"],
                           chunk_text=h["text"], distance=d, url=h["url"]) for h, d in zip(hits, distances)]


def retrieve_fts(query_text: str, top_k: int = DEFAULT_TOP_K) -> list[RetrievedChunk]:
    """Top posts and prep notes by bm25. card_id holds the source id for posts and
    -<note id> for your own stories (no card behind them)."""
    query = _or_query(query_text)
    if not query:
        return []
    conn = get_connection()
    try:
        scored = [(sc, _post_hit(conn, i)) for i, sc in _fts_posts(conn, query, top_k)]
        scored += [(sc, _note_hit(conn, i)) for i, sc in _fts_notes(conn, query, top_k)]
    finally:
        conn.close()
    scored = sorted([x for x in scored if x[1]], key=lambda x: x[0])[:top_k]
    return _chunks([h for _, h in scored], _bm25_to_distance([sc for sc, _ in scored]))


# --- hybrid: stored embeddings (cosine) + FTS (bm25), merged by rank ---------------------

_RRF_K = 60          # reciprocal rank fusion constant: score = sum(1 / (K + rank))
_CANDIDATES = 20     # how deep each ranked list goes before fusion


def fuse_ranks(lists: list, k: int = _RRF_K) -> list[tuple[tuple[str, int], float]]:
    """Weighted reciprocal rank fusion: score = sum(weight / (k + rank)).

    Each entry is a ranked list of keys, or (ranked list, weight). Items high in several
    lists win; ties keep first-seen order."""
    scores: dict[tuple[str, int], float] = {}
    for entry in lists:
        ranked, weight = entry if isinstance(entry, tuple) else (entry, 1.0)
        for rank, key in enumerate(ranked, start=1):
            scores[key] = scores.get(key, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def retrieve_hybrid(query_text: str, top_k: int = DEFAULT_TOP_K, embedder=None,
                    now: int | None = None) -> list[RetrievedChunk] | None:
    """Meaning (cosine over stored vectors) + keywords (bm25), fused by rank.

    Returns None when hybrid can't run (no stored vectors, no OPENAI_API_KEY, API error,
    or the daily cap) so the caller falls back to FTS only. New or changed posts and
    notes are embedded first, so the index stays current; costs go to outputs.
    """
    from contentpilot.copilot import embeddings

    conn = get_connection()
    try:
        if not embeddings.has_embeddings(conn) or (embedder is None and not embeddings.enabled()):
            return None
        try:
            embeddings.embed_pending(conn, embedder=embedder, now=now)
            qvec = embeddings.embed_query(conn, query_text, embedder=embedder, now=now)
            conn.commit()
        except Exception:
            conn.rollback()
            return None
        # Meaning: ONE list over posts and notes. Cosine scores share a scale, so a post that
        # is merely the least-bad post can't take first place the way it would in a posts-only list.
        semantic = sorted(
            [(sim, ("post", i)) for i, sim in embeddings.rank(conn, "post", qvec, _CANDIDATES)]
            + [(sim, ("note", i)) for i, sim in embeddings.rank(conn, "prep_note", qvec, _CANDIDATES)],
            key=lambda x: -x[0])[:_CANDIDATES]
        lists = [([key for _, key in semantic], 1.0)]
        # Keywords: two lists, because bm25 from the big posts index and the tiny notes index
        # are not comparable. Half weight each, so keywords as a whole count as much as meaning.
        query = _or_query(query_text)
        if query:
            lists.append(([("post", i) for i, _ in _fts_posts(conn, query, _CANDIDATES)], 0.5))
            lists.append(([("note", i) for i, _ in _fts_notes(conn, query, _CANDIDATES)], 0.5))
        fused = []
        for (kind, item_id), score in fuse_ranks(lists):
            hit = _post_hit(conn, item_id) if kind == "post" else _note_hit(conn, item_id)
            if hit:
                fused.append((score, hit))
            if len(fused) == top_k:
                break
    finally:
        conn.close()
    if not fused:
        return []
    best = fused[0][0]
    return _chunks([h for _, h in fused], [1.0 - sc / best for sc, _ in fused])


def retrieve(
    query_text: str,
    top_k: int = DEFAULT_TOP_K,
    filter_topics: list[str] | None = None,
) -> list[RetrievedChunk]:
    """Hybrid (embeddings + FTS) when vectors exist, else FTS only.
    CONTENTPILOT_SEARCH=fts forces FTS; CONTENTPILOT_SEARCH=chroma uses the legacy Chroma index."""
    mode = os.environ.get("CONTENTPILOT_SEARCH", "").strip().lower()
    if use_fts_only():
        if mode != "fts":
            chunks = retrieve_hybrid(query_text, top_k)
            if chunks is not None:
                return chunks
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
