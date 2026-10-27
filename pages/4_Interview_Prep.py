from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

from contentpilot.rag.gap_detector import get_coverage
from contentpilot.rag.retriever import retrieve
from contentpilot.rag.synthesizer import SynthesisValidationError, synthesize

load_dotenv()

st.set_page_config(page_title="Interview Prep — ContentPilot", page_icon="🎯")
st.title("Interview Prep")
st.caption("Ask an interview question — answers are synthesized from your ingested library, with sources cited.")

CONFIDENCE_COLORS = {"high": "🟢", "medium": "🟡", "low": "🔴"}

if "interview_history" not in st.session_state:
    st.session_state.interview_history = []  # list of {query, chunks, result}

if "coverage" not in st.session_state:
    with st.spinner("Checking topic coverage..."):
        st.session_state.coverage = get_coverage()

if "pending_query" not in st.session_state:
    st.session_state.pending_query = ""


def render_coverage(coverage) -> None:
    with st.expander(f"Topic coverage: {coverage.coverage_pct:.0f}%", expanded=False):
        if coverage.covered_topics:
            st.markdown("**You have strong coverage on:**")
            st.markdown(" ".join(f"`{t}`" for t in coverage.covered_topics))
        if coverage.gap_topics:
            st.markdown("**Gaps:**")
            st.markdown(" ".join(f":red[`{t}`]" for t in coverage.gap_topics))


def render_answer(query: str, result) -> None:
    badge = CONFIDENCE_COLORS.get(result.confidence, "⚪")
    st.markdown(f"{badge} **Confidence: {result.confidence}**")

    with st.container(border=True):
        st.markdown(result.answer)

    st.markdown("**Key points**")
    for point in result.key_points:
        st.markdown(f"- {point}")

    if result.gaps:
        st.markdown("**Gaps in your library for this question**")
        for gap in result.gaps:
            st.markdown(f"- {gap}")

    with st.expander(f"Sources ({len(result.sources_used)})"):
        for source in result.sources_used:
            st.markdown(f"- **{source.author}** — [{source.title}]({source.url})")

    st.markdown("**Dig deeper**")
    cols = st.columns(len(result.follow_up_questions) or 1)
    for col, question in zip(cols, result.follow_up_questions):
        if col.button(question, key=f"followup_{hash(query + question)}"):
            st.session_state.pending_query = question
            st.rerun()


def run_query(query: str) -> None:
    with st.spinner("Retrieving relevant insights..."):
        chunks = retrieve(query)

    if not chunks:
        st.warning("No relevant insights found in your library for this question.")
        return

    with st.spinner(f"Synthesizing answer from {len(chunks)} source(s)..."):
        try:
            result = synthesize(query, chunks)
        except SynthesisValidationError as exc:
            st.error(f"Synthesis failed validation even after retry: {exc}")
            return

    st.session_state.interview_history.insert(0, {"query": query, "chunks": chunks, "result": result})
    st.session_state.interview_history = st.session_state.interview_history[:5]


render_coverage(st.session_state.coverage)
st.divider()

query = st.text_input("Ask an interview question or topic", value=st.session_state.pending_query)
st.session_state.pending_query = ""

if st.button("Ask", disabled=not query):
    run_query(query)

if st.session_state.interview_history:
    latest = st.session_state.interview_history[0]
    st.subheader(latest["query"])
    render_answer(latest["query"], latest["result"])

    markdown_lines = [f"# {latest['query']}", "", latest["result"].answer, "", "## Key Points"]
    markdown_lines += [f"- {p}" for p in latest["result"].key_points]
    markdown_lines += ["", "## Sources"]
    markdown_lines += [
        f"- {s.author} — [{s.title}]({s.url})" for s in latest["result"].sources_used
    ]
    markdown_export = "\n".join(markdown_lines)

    st.download_button(
        "Export to Markdown",
        data=markdown_export,
        file_name=f"interview-prep-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md",
        mime="text/markdown",
    )

if len(st.session_state.interview_history) > 1:
    st.divider()
    st.subheader("History")
    for entry in st.session_state.interview_history[1:]:
        with st.expander(entry["query"]):
            render_answer(entry["query"], entry["result"])
