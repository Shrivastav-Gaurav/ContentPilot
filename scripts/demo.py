"""Non-interactive end-to-end demo — what to run on screen during a
recruiter call. Exercises the real pipeline (gap detection, retrieval,
synthesis) against whatever is already in data/. Run with:
    python -m scripts.demo
"""
import sys

from dotenv import load_dotenv

from contentpilot.rag.gap_detector import get_coverage
from contentpilot.rag.retriever import retrieve
from contentpilot.rag.synthesizer import synthesize

DEMO_QUERY = "How do Amazon PMs think about customer obsession vs. metrics?"


def main() -> None:
    load_dotenv()

    print("ContentPilot Demo")
    print("=" * 40)

    print("\n[1/3] Checking topic coverage...")
    coverage = get_coverage()
    top_gaps = coverage.gap_topics[:3]
    print(f"  Coverage: {coverage.coverage_pct:.0f}%")
    print(f"  Top gaps: {', '.join(top_gaps) if top_gaps else '(none)'}")

    print(f"\n[2/3] Retrieving insights for: {DEMO_QUERY!r}")
    chunks = retrieve(DEMO_QUERY)
    print(f"  Chunks retrieved: {len(chunks)}")
    if chunks:
        top = chunks[0]
        print(f"  Top source: {top.source_title!r} (distance={top.distance:.3f})")

    print("\n[3/3] Synthesizing answer...")
    if not chunks:
        print("  No chunks available — skipping synthesis.")
    else:
        result = synthesize(DEMO_QUERY, chunks)
        summary = result.answer[:300]
        print(f"  Answer (first 300 chars): {summary}...")
        print(f"  Confidence: {result.confidence}")
        print(f"  Sources used: {len(result.sources_used)}")
        for source in result.sources_used[:3]:
            print(f"    - {source.author}: {source.title}")

    # Windows' default console codepage (cp1252) can't encode U+2713; fall
    # back to an ASCII equivalent rather than crash the very last line of
    # what's supposed to be a clean, screen-shareable demo run.
    checkmark = "✓"
    try:
        checkmark.encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        checkmark = "[OK]"
    print(f"\n{checkmark} Pipeline working end-to-end")


if __name__ == "__main__":
    main()
