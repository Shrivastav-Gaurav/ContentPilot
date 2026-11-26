"""Checks how well the ingested card library covers core TPM interview
topics, using semantic similarity against the Chroma index rather than exact
tag matching — ingested topics are free-text LLM output ("gradient descent",
"neural networks"), not a fixed taxonomy, so substring/exact matching against
CORE_TPM_TOPICS would almost never hit even when a card is genuinely
relevant.
"""
from dataclasses import dataclass

from contentpilot.copilot.topics import CORE_TPM_TOPICS  # noqa: F401  (re-exported)
from contentpilot.storage import _get_chroma_collection


# Cosine distance threshold below which a card counts as "covering" a topic.
# Tuned against real data: a tight topical match (e.g. "neural networks"
# against an ML tutorial) lands around 0.25-0.4; generic business/strategy
# content that's merely adjacent to a TPM topic (e.g. a PRD template matching
# "Amazon Leadership Principles") sits around 0.55-0.65. 0.75 let nearly
# everything through — every one of the 20 core topics came back "covered,"
# which contradicted even the synthesizer's own assessment that the library
# doesn't actually address Amazon-specific material. 0.45 requires genuine
# topical closeness, not just "same general domain."
_COVERAGE_DISTANCE_THRESHOLD = 0.45
_MIN_CARDS_FOR_COVERAGE = 2


@dataclass
class CoverageResult:
    covered_topics: list[str]
    gap_topics: list[str]
    coverage_pct: float


def _cards_covering_topic(topic: str) -> int:
    collection = _get_chroma_collection()
    result = collection.query(
        query_texts=[topic],
        n_results=10,
        include=["metadatas", "distances"],
    )

    distances = result.get("distances", [[]])[0]
    metadatas = result.get("metadatas", [[]])[0]

    card_ids = set()
    for distance, metadata in zip(distances, metadatas):
        if distance <= _COVERAGE_DISTANCE_THRESHOLD:
            card_ids.add(metadata["card_id"])
    return len(card_ids)


def get_coverage() -> CoverageResult:
    covered, gaps = [], []

    for topic in CORE_TPM_TOPICS:
        try:
            count = _cards_covering_topic(topic)
        except Exception:
            count = 0
        if count >= _MIN_CARDS_FOR_COVERAGE:
            covered.append(topic)
        else:
            gaps.append(topic)

    coverage_pct = (len(covered) / len(CORE_TPM_TOPICS)) * 100 if CORE_TPM_TOPICS else 0.0

    return CoverageResult(covered_topics=covered, gap_topics=gaps, coverage_pct=coverage_pct)
