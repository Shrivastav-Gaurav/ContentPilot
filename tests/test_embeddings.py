"""Meaning-based search without Chroma: stored vectors, hybrid retrieval, Search by meaning.

A fake embedder maps text to a small fixed set of 4-d vectors, so no API calls are made:
    [alignment / stakeholders, courses / bootcamps, retrieval / RAG, anything else]
"""
import pytest

from test_copilot import DAY, JINA, NOW, conn, fake_llm_factory  # noqa: F401  (conn is a fixture)

QUESTION = "How do you get engineering and product to agree on a roadmap?"
COURSE_TITLE = "30-Day AI Agent Mastery Coding Course by Systemdr | System Design Roadmap posted on the topic | LinkedIn"
COURSE_BODY = ("Follow this roadmap: a 30-day course. The roadmap covers agents, tools and memory. "
               "Day 1 of the roadmap: build your first agent. Enroll in the course today.")
CONSENSUS = "Driving consensus across disagreeing stakeholders"
STORY_1 = "Got engineering and product aligned on one roadmap by agreeing on success metrics before features."
STORY_2 = "Stakeholder alignment: a pre-mortem helped product and engineering agree on trade-offs early."


class FakeEmbedder:
    def __init__(self):
        self.calls, self.inputs = 0, []

    @staticmethod
    def vector(text: str) -> list[float]:
        t = text.lower()
        if "how do you get engineering and product" in t:      # the question
            return [0.9, 0.25, 0.0, 0.1]
        if "course" in t or "enroll" in t:
            return [0.05, 1.0, 0.0, 0.0]
        if "align" in t or "stakeholder" in t or "consensus" in t:
            return [1.0, 0.0, 0.0, 0.05]
        if "retrieval" in t or "rag" in t:
            return [0.0, 0.0, 1.0, 0.0]
        return [0.0, 0.0, 0.0, 1.0]

    def __call__(self, texts):
        self.calls += 1
        self.inputs += texts
        return [self.vector(t) for t in texts], 10 * len(texts)


@pytest.fixture()
def library(conn):
    """The 6 fixture posts + the 'System Design Roadmap' course + two alignment stories."""
    from contentpilot.copilot import service
    conn.execute(
        "INSERT INTO sources (source_type, canonical_url, raw_text, fetched_at, saved_at) VALUES ('linkedin', ?, ?, ?, ?)",
        ("https://li/course", JINA.format(title=COURSE_TITLE, n=7, published="2026-11-01T10:00:00.000Z",
                                          author="System Design Roadmap", body=COURSE_BODY), NOW - DAY, NOW - DAY))
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    service.seed_prep_topics(conn)
    tid = {t["name"]: t["id"] for t in service.list_prep_topics(conn)}[CONSENSUS]
    service.add_story(conn, tid, STORY_1, now=NOW)
    service.add_story(conn, tid, STORY_2, now=NOW + 1)
    conn.commit()
    return conn


def _embed_all(conn, embedder=None):
    from contentpilot.copilot import embeddings
    stats = embeddings.embed_pending(conn, embedder=embedder or FakeEmbedder(), now=NOW, daily_cap_usd=1.0)
    conn.commit()
    return stats


# --- storage, re-embedding, cost -----------------------------------------------------------

def test_embed_once_and_only_changed_text_again(library):
    from contentpilot.copilot import embeddings, service
    fake = FakeEmbedder()
    spent_before = service.spent_today(library, NOW)                    # the fixture's fake triage cost
    stats = _embed_all(library, fake)
    assert stats["embedded"] == 9 and stats["calls"] == 1              # 7 posts + 2 notes, one batch
    assert embeddings.counts(library) == {"post": 7, "prep_note": 2}
    # Input = display title + summary + start of body (the course's LinkedIn suffix is stripped).
    course_input = next(t for t in fake.inputs if "Enroll" in t)
    assert course_input.startswith("30-Day AI Agent Mastery Coding Course by Systemdr\n")
    assert "posted on the topic" not in course_input

    assert _embed_all(library, fake)["embedded"] == 0 and fake.calls == 1    # unchanged: no call

    library.execute("UPDATE prep_notes SET text = text || ' Result: shipped on time.' WHERE id = "
                    "(SELECT MIN(id) FROM prep_notes)")
    stats = _embed_all(library, fake)
    assert stats["embedded"] == 1 and fake.calls == 2                   # only the edited note

    # Cost is in outputs, so spent_today counts it: 90 + 10 tokens at $0.02 / 1M.
    assert service.spent_today(library, NOW) - spent_before == pytest.approx(100 * 0.02 / 1_000_000)
    assert library.execute("SELECT COUNT(*) FROM outputs WHERE kind = 'embedding'").fetchone()[0] == 2


def test_embedding_stops_at_cap_and_drops_orphan_notes(library):
    from contentpilot.copilot import embeddings
    fake = FakeEmbedder()
    stats = embeddings.embed_pending(library, embedder=fake, now=NOW, daily_cap_usd=0.0)
    assert stats["stopped_by_cap"] and fake.calls == 0
    _embed_all(library, fake)
    library.execute("DELETE FROM prep_notes WHERE id = (SELECT MIN(id) FROM prep_notes)")
    assert embeddings.embed_pending(library, embedder=fake, now=NOW, daily_cap_usd=1.0)["removed"] == 1
    assert embeddings.counts(library)["prep_note"] == 1


def test_query_vectors_are_cached(library):
    from contentpilot.copilot import embeddings
    from contentpilot.copilot.spend import DailyCapReached
    fake = FakeEmbedder()
    v1 = embeddings.embed_query(library, QUESTION, embedder=fake, now=NOW)
    v2 = embeddings.embed_query(library, "  " + QUESTION.upper(), embedder=fake, now=NOW)
    assert fake.calls == 1 and list(v1) == list(v2)
    assert library.execute("SELECT COUNT(*) FROM outputs WHERE kind = 'query_embedding'").fetchone()[0] == 1
    with pytest.raises(DailyCapReached):
        embeddings.embed_query(library, "a new question", embedder=fake, now=NOW, daily_cap_usd=0.0)


# --- Ask: hybrid retrieval ------------------------------------------------------------------

def test_roadmap_question_ranks_alignment_notes_above_the_course(library):
    from contentpilot.rag import retriever
    _embed_all(library)
    chunks = retriever.retrieve_hybrid(QUESTION, embedder=FakeEmbedder(), now=NOW)
    titles = [c.source_title for c in chunks]
    course = next(i for i, c in enumerate(chunks) if c.url == "https://li/course")
    stories = [i for i, c in enumerate(chunks) if c.author_name == "You"]
    assert len(stories) == 2 and max(stories) < course, titles
    assert chunks[0].source_title == f"My story: {CONSENSUS}" and chunks[0].distance == 0.0
    assert all(0.0 <= c.distance <= 1.0 for c in chunks) and len(chunks) <= 8

    # Keywords alone put the course ahead of at least one alignment story: that is what hybrid fixes.
    fts = retriever.retrieve_fts(QUESTION)
    fts_course = next(i for i, c in enumerate(fts) if c.url == "https://li/course")
    assert any(i > fts_course for i, c in enumerate(fts) if c.author_name == "You")


def test_retrieve_uses_hybrid_when_vectors_exist_else_fts(library, monkeypatch):
    from contentpilot import config
    from contentpilot.copilot import embeddings
    from contentpilot.rag import retriever
    monkeypatch.delenv("CONTENTPILOT_SEARCH", raising=False)
    fake = FakeEmbedder()
    monkeypatch.setattr(config, "openai_api_key", lambda: "test-key")
    monkeypatch.setattr(embeddings, "openai_embedder", fake)
    monkeypatch.setattr(retriever, "_semantic_search", lambda *a, **k: pytest.fail("Chroma must not be used"))

    no_vectors = retriever.retrieve(QUESTION)                      # no vectors yet: FTS only, no calls
    assert no_vectors and fake.calls == 0

    _embed_all(library, fake)
    calls_before = fake.calls
    hybrid = retriever.retrieve(QUESTION)
    assert hybrid[0].author_name == "You" and fake.calls == calls_before + 1   # just the query vector

    monkeypatch.setenv("CONTENTPILOT_SEARCH", "fts")                 # forced FTS: no embedding call
    assert retriever.retrieve(QUESTION) == retriever.retrieve_fts(QUESTION) and fake.calls == calls_before + 1

    monkeypatch.delenv("CONTENTPILOT_SEARCH")
    monkeypatch.setattr(config, "openai_api_key", lambda: None)      # key removed: FTS fallback
    assert retriever.retrieve_hybrid(QUESTION) is None


def test_hybrid_falls_back_when_the_api_fails(library):
    from contentpilot.rag import retriever
    _embed_all(library)

    def broken(texts):
        raise ConnectionError("api down")
    assert retriever.retrieve_hybrid("a brand new question", embedder=broken, now=NOW) is None


def test_fuse_ranks():
    from contentpilot.rag.retriever import fuse_ranks
    fused = [k for k, _ in fuse_ranks([[("a", 1), ("b", 2)], [("b", 2), ("c", 3)], [("b", 2)]])]
    assert fused == [("b", 2), ("a", 1), ("c", 3)]
    # Weighted: first in a half-weight list loses to second in a full-weight list.
    fused = [k for k, _ in fuse_ranks([([("x", 1), ("y", 2)], 1.0), ([("y", 2), ("z", 3)], 0.5),
                                       ([("z", 3)], 0.5)])]
    assert fused == [("y", 2), ("x", 1), ("z", 3)]


# --- Search by meaning ----------------------------------------------------------------------------

def test_search_by_meaning_ranks_and_filters(library):
    from contentpilot.copilot import search
    _embed_all(library)
    fake = FakeEmbedder()
    results = search.search(library, "how do you get engineering and product to agree", by_meaning=True,
                            embedder=fake, now=NOW)
    assert results[0]["id"] == 7 or results[0]["similarity"] >= results[1]["similarity"]
    assert [r["similarity"] for r in results] == sorted((r["similarity"] for r in results), reverse=True)
    assert len(results) == 7

    rag = search.search(library, "retrieval augmented generation", by_meaning=True, embedder=FakeEmbedder(), now=NOW)
    assert rag[0]["id"] == 3                                       # "Hybrid retrieval and reranking"

    dismissed = search.search(library, "retrieval", status="dismissed", by_meaning=True, embedder=fake, now=NOW)
    assert {r["id"] for r in dismissed} == {4}                     # filters still apply
    assert search.meaning_search_ready(library) is False or True   # depends on the key; never raises
