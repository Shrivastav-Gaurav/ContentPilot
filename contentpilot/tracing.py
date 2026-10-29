"""Optional LangSmith tracing for the two LLM calls that matter for
observability: card extraction and interview-answer synthesis.

If LANGSMITH_API_KEY isn't set, trace_llm_call is a no-op context manager —
callers don't need to branch on whether tracing is configured.
"""
import os
from contextlib import contextmanager


def _tracing_enabled() -> bool:
    return bool(os.environ.get("LANGSMITH_API_KEY", "").strip())


@contextmanager
def trace_llm_call(name: str, **tags):
    """Wraps an LLM call in a LangSmith trace named `name`, with `tags` as
    trace metadata. No-ops silently if LANGSMITH_API_KEY isn't set, or if the
    langsmith package isn't importable, or if LangSmith itself errors —
    tracing must never be able to break the actual LLM call it wraps.
    """
    if not _tracing_enabled():
        yield
        return

    try:
        from langsmith import trace
        tracer = trace(
            name=name,
            project_name=os.environ.get("LANGSMITH_PROJECT", "contentpilot"),
            metadata=tags,
        )
        tracer.__enter__()
    except Exception:
        # Tracing is observability, not a dependency of correctness — a
        # LangSmith outage, missing package, or misconfiguration should
        # never prevent the actual extraction/synthesis call from running.
        yield
        return

    try:
        yield
    finally:
        try:
            tracer.__exit__(None, None, None)
        except Exception:
            pass
