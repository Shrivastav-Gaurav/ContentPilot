import streamlit as st
from dotenv import load_dotenv

from contentpilot import config
from contentpilot.copilot import service
from contentpilot.copilot.rules import TYPE_LABELS
from contentpilot.fetchers.youtube_channel import is_channel_url, list_video_urls
from contentpilot.storage import (
    create_ingest_job,
    get_ingest_job,
    job_item_status_counts,
    list_ingest_jobs,
    list_job_items,
    request_job_cancel,
    retry_failed_items,
)

load_dotenv()

st.set_page_config(page_title="ContentPilot", page_icon="🎯")
st.title("ContentPilot")

with service.transaction() as conn:
    counts = service.home_counts(conn)

c1, c2, c3 = st.columns(3)
c1.metric("Open", counts["open_total"])
c2.metric("Overdue", counts["overdue"])
c3.metric("Done this week", counts["done_this_week"])
st.caption(" · ".join(f"{TYPE_LABELS[t]}: {n}" for t, n in counts["open_by_type"].items() if t in TYPE_LABELS))

st.page_link("pages/1_Inbox.py", label="Inbox: sort what's waiting", icon="📥")
st.page_link("pages/2_Search.py", label="Search everything you've saved", icon="🔎")
st.page_link("pages/3_Done_log.py", label="Done log: what you did, and undo", icon="✅")
st.page_link("pages/4_Interview_Prep.py", label="Interview Prep", icon="🎯")

st.divider()

with st.expander("YouTube batch ingest"):
    st.caption("Ingest a YouTube channel, playlist or video through the background worker "
               "(`python -m contentpilot.worker`).")

    with st.form("ingest_form"):
        ingest_target = st.text_input("Channel / playlist / video URL")
        col1, col2 = st.columns(2)
        with col1:
            max_videos = st.number_input("Max videos", min_value=1, max_value=200, value=config.DEFAULT_MAX_VIDEOS)
        with col2:
            cost_cap = st.number_input("Cost cap (USD)", min_value=0.05, max_value=50.0,
                                       value=config.DEFAULT_COST_CAP_USD, step=0.25)
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
                st.success(f"Started job #{job_id} with {len(urls)} item(s). Make sure the worker is running.")
        except Exception as exc:
            st.error(f"Could not start job: {exc}")

    jobs = list_ingest_jobs(limit=10)
    if not jobs:
        st.caption("No jobs yet.")
    else:
        selected_job_id = st.selectbox(
            "Recent jobs", options=[j["id"] for j in jobs],
            format_func=lambda jid: next(f"#{j['id']} — {j['target']} ({j['status']})" for j in jobs if j["id"] == jid),
        )
        job = get_ingest_job(selected_job_id)

        if job:
            item_counts = job_item_status_counts(selected_job_id)
            st.write(f"**Status:** {job['status']}  |  **Cost so far:** ${job['cost_usd']:.4f} / ${job['cost_cap_usd']:.2f}")
            st.write(" / ".join(f"{status}: {n}" for status, n in sorted(item_counts.items())) or "No items yet.")

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                if job["status"] in ("queued", "running") and st.button("Cancel job"):
                    request_job_cancel(selected_job_id)
                    st.rerun()
            with col_b:
                if item_counts.get("failed") and st.button("Retry failed items"):
                    retry_failed_items(selected_job_id)
                    st.rerun()
            with col_c:
                if st.button("Refresh"):
                    st.rerun()

            items_box = st.container(height=240)  # expanders can't nest, so scroll instead
            for item in list_job_items(selected_job_id):
                items_box.text(f"[{item['status']}] {item['source_url']}" + (f" — {item['error']}" if item["error"] else ""))
