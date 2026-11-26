import streamlit as st
from dotenv import load_dotenv

from contentpilot.rag.card_chat_agent import STARTERS, chat
from contentpilot.storage import clear_chat_history, list_saved_cards, load_chat_history

load_dotenv()

st.set_page_config(page_title="Saved — ContentPilot", page_icon="📌")
st.title("Saved")
st.caption("Cards from your LinkedIn saved posts. Talk to each one — ask it to explain, quiz you, or find interview angles.")

STARTER_LABELS = {"study": "📚 Study", "build": "🔨 Build", "apply": "💼 Apply", "share": "📤 Share"}

if "open_chat_card_id" not in st.session_state:
    st.session_state.open_chat_card_id = None
if "pending_starter" not in st.session_state:
    st.session_state.pending_starter = None

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

for card in cards:
    extraction = card["extraction"]
    card_id = card["id"]
    title = extraction.get("title") or card.get("source_title") or "Untitled"
    is_open = st.session_state.open_chat_card_id == card_id

    with st.expander(f"{title}  —  {extraction.get('summary', '')[:80]}", expanded=is_open):
        st.caption(f"[Source]({card['canonical_url']})")

        if not is_open:
            cols = st.columns(len(STARTERS))
            for col, flag in zip(cols, STARTERS):
                if col.button(STARTER_LABELS[flag], key=f"open_{card_id}_{flag}"):
                    st.session_state.open_chat_card_id = card_id
                    st.session_state.pending_starter = STARTERS[flag]
                    st.rerun()
        else:
            history = load_chat_history(card_id)
            for turn in history:
                with st.chat_message(turn["role"]):
                    st.markdown(turn["content"])

            col_close, col_clear = st.columns(2)
            if col_close.button("Close chat", key=f"close_{card_id}"):
                st.session_state.open_chat_card_id = None
                st.session_state.pending_starter = None
                st.rerun()
            if col_clear.button("Clear chat", key=f"clear_{card_id}"):
                clear_chat_history(card_id)
                st.rerun()

            pending = st.session_state.pending_starter
            if pending:
                st.session_state.pending_starter = None
                with st.chat_message("user"):
                    st.markdown(pending)
                with st.spinner("Thinking..."):
                    reply = chat(card_id, pending, history=history)
                with st.chat_message("assistant"):
                    st.markdown(reply)
                st.rerun()

            user_message = st.chat_input("Ask anything about this post...", key=f"input_{card_id}")
            if user_message:
                with st.chat_message("user"):
                    st.markdown(user_message)
                with st.spinner("Thinking..."):
                    reply = chat(card_id, user_message, history=history)
                with st.chat_message("assistant"):
                    st.markdown(reply)
                st.rerun()
