import streamlit as st
from dotenv import load_dotenv

from contentpilot import config
from contentpilot.storage import VALID_FLAGS, list_saved_cards, toggle_card_flag

load_dotenv()

st.set_page_config(page_title="Saved — ContentPilot", page_icon="📌")
st.title("Saved")
st.caption("Cards from your LinkedIn saved posts, newest scan first.")

FLAG_LABELS = {"study": "📚 Study", "build": "🔨 Build", "apply": "💼 Apply", "share": "📤 Share"}

if st.button("Scan LinkedIn Saved Now"):
    try:
        from contentpilot.worker import trigger_saved_scan

        with st.spinner("Opening LinkedIn and scanning saved posts... (a browser window will appear)"):
            job_id = trigger_saved_scan()
        st.success(f"Scan started — job #{job_id} enqueued. The worker will process it; refresh this page shortly.")
    except Exception as exc:
        st.error(f"Could not start scan: {exc}")

st.divider()

cards = list_saved_cards()

if not cards:
    st.caption("No saved cards yet. Run a scan above, or wait for the scheduled weekly scan.")
else:
    for card in cards:
        extraction = card["extraction"]
        with st.container(border=True):
            st.subheader(extraction.get("title") or card.get("source_title") or "Untitled")
            st.caption(extraction.get("summary", ""))
            st.caption(f"[Source]({card['canonical_url']})")

            cols = st.columns(len(VALID_FLAGS))
            for col, flag in zip(cols, VALID_FLAGS):
                is_set = flag in card["flags"]
                label = FLAG_LABELS[flag] + (" ✓" if is_set else "")
                if col.button(label, key=f"flag_{card['id']}_{flag}"):
                    toggle_card_flag(card["id"], flag)
                    st.rerun()

            with st.expander("Details"):
                st.markdown("**Key Learnings**")
                for item in extraction["key_learnings"]:
                    st.markdown(f"- {item}")
                if extraction["build_ideas"]:
                    st.markdown("**Build Ideas**")
                    for item in extraction["build_ideas"]:
                        st.markdown(f"- {item}")
                if extraction["job_signals"]:
                    st.markdown("**Job Signals**")
                    for item in extraction["job_signals"]:
                        st.markdown(f"- {item}")
