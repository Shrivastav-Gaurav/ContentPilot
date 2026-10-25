import streamlit as st
from dotenv import load_dotenv

from contentpilot import config
from contentpilot.extraction import ExtractionValidationError, extract_tpm_lens
from contentpilot.fetchers.youtube_channel import is_channel_url, list_video_urls
from contentpilot.prompts import tpm_lens
from contentpilot.storage import (
    create_ingest_job,
    get_full_card,
    get_ingest_job,
    get_source_by_url,
    job_item_status_counts,
    list_ingest_jobs,
    list_job_items,
    request_job_cancel,
    retry_failed_items,
    save_card,
    save_source,
)
from contentpilot.transcript import fetch_transcript

load_dotenv()

LENS = "tpm"

st.set_page_config(page_title="ContentPilot", page_icon="🎯")
st.title("ContentPilot")
st.caption("Paste a YouTube URL to extract TPM-lens learnings, build ideas, and job signals.")

url = st.text_input("YouTube URL")


def render_card(extraction: dict) -> None:
    with st.container(border=True):
        st.subheader(extraction.get("title") or "Untitled")
        st.caption(extraction.get("summary", ""))

        st.subheader("Key Learnings")
        for item in extraction["key_learnings"]:
            st.markdown(f"- {item}")

        st.subheader("Build Ideas")
        for item in extraction["build_ideas"]:
            st.markdown(f"- {item}")

        if extraction["job_signals"]:
            st.subheader("Job Signals")
            for item in extraction["job_signals"]:
                st.markdown(f"- {item}")


if st.button("Extract", disabled=not url):
    try:
        with st.spinner("Fetching transcript..."):
            raw = fetch_transcript(url)

        existing_source = get_source_by_url(raw.canonical_url)
        source_id = existing_source["id"] if existing_source else None

        cached_card = None
        if source_id:
            cached_card = get_full_card(source_id, LENS, tpm_lens.VERSION)

        if cached_card:
            st.info("Loaded from cache.")
            render_card(cached_card["extraction"])
        else:
            source_id = save_source(raw)
            with st.spinner("Extracting TPM-lens insights via OpenRouter..."):
                result = extract_tpm_lens(raw.text)
            save_card(source_id, LENS, result)
            render_card(result.extraction.model_dump())

    except ValueError as exc:
        st.error(str(exc))
    except ExtractionValidationError as exc:
        st.error(f"Extraction failed validation even after retry: {exc}")
    except Exception as exc:
        st.error(f"Something went wrong: {exc}")


st.divider()
st.header("Batch ingest")
st.caption("Ingest a YouTube channel/playlist, or a batch of URLs, via a background worker.")

with st.form("ingest_form"):
    ingest_target = st.text_input("Channel / playlist / video URL")
    col1, col2 = st.columns(2)
    with col1:
        max_videos = st.number_input("Max videos", min_value=1, max_value=200, value=config.DEFAULT_MAX_VIDEOS)
    with col2:
        cost_cap = st.number_input("Cost cap (USD)", min_value=0.05, max_value=50.0, value=config.DEFAULT_COST_CAP_USD, step=0.25)
    submitted = st.form_submit_button("Start ingest job")

if submitted and ingest_target:
    try:
        if is_channel_url(ingest_target):
            urls = list_video_urls(ingest_target, max_videos=int(max_videos))
            job_type = "channel"
        else:
            urls = [ingest_target]
            job_type = "single_url"

        if not urls:
            st.error("No videos found for that URL.")
        else:
            job_id = create_ingest_job(job_type, ingest_target, urls, cost_cap_usd=float(cost_cap))
            st.success(f"Started job #{job_id} with {len(urls)} item(s). Make sure the worker is running: "
                       "`python -m contentpilot.worker`")
    except Exception as exc:
        st.error(f"Could not start job: {exc}")

st.subheader("Recent jobs")
jobs = list_ingest_jobs(limit=10)

if not jobs:
    st.caption("No jobs yet.")
else:
    selected_job_id = st.selectbox(
        "Job", options=[j["id"] for j in jobs],
        format_func=lambda jid: next(f"#{j['id']} — {j['target']} ({j['status']})" for j in jobs if j["id"] == jid),
    )
    job = get_ingest_job(selected_job_id)

    if job:
        counts = job_item_status_counts(selected_job_id)
        st.write(f"**Status:** {job['status']}  |  **Cost so far:** ${job['cost_usd']:.4f} / ${job['cost_cap_usd']:.2f}")
        st.write(" / ".join(f"{status}: {n}" for status, n in sorted(counts.items())) or "No items yet.")

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            if job["status"] in ("queued", "running") and st.button("Cancel job"):
                request_job_cancel(selected_job_id)
                st.rerun()
        with col_b:
            if counts.get("failed") and st.button("Retry failed items"):
                retry_failed_items(selected_job_id)
                st.rerun()
        with col_c:
            if st.button("Refresh"):
                st.rerun()

        with st.expander("Item details"):
            for item in list_job_items(selected_job_id):
                st.text(f"[{item['status']}] {item['source_url']}" + (f" — {item['error']}" if item["error"] else ""))

        if job["status"] in ("queued", "running"):
            st.caption("Auto-refreshing while the job is active...")
            import time as _time
            _time.sleep(3)
            st.rerun()


st.divider()
st.header("Paste text manually")
st.caption("For LinkedIn posts or articles the fetcher can't read (login walls, etc.).")

with st.form("manual_paste_form"):
    manual_url = st.text_input("Source URL")
    manual_text = st.text_area("Paste the post/article text here", height=150)
    manual_submitted = st.form_submit_button("Extract from pasted text")

if manual_submitted and manual_url and manual_text:
    try:
        from contentpilot.fetchers.base import RawContent
        from contentpilot.fetchers.jina import canonicalize

        raw = RawContent(
            source_type="linkedin",
            external_id=manual_url,
            canonical_url=canonicalize(manual_url),
            title=None,
            author_name=None,
            author_channel_id=None,
            author_url=None,
            published_at=None,
            duration_sec=None,
            language=None,
            text=manual_text,
            fetch_status="manual",
        )
        source_id = save_source(raw)
        with st.spinner("Extracting TPM-lens insights via OpenRouter..."):
            result = extract_tpm_lens(raw.text)
        save_card(source_id, LENS, result)
        render_card(result.extraction.model_dump())
    except ExtractionValidationError as exc:
        st.error(f"Extraction failed validation even after retry: {exc}")
    except Exception as exc:
        st.error(f"Something went wrong: {exc}")
