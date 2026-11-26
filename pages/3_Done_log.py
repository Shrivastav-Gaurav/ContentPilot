"""Done log: everything you (or the rules) did with a post, newest first."""
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

from contentpilot.copilot import service
from contentpilot.copilot.display import action_label, md

load_dotenv()

st.set_page_config(page_title="Done log — ContentPilot", page_icon="✅")
st.title("Done log")

st.session_state.setdefault("done_flash", None)


def do_move_back(source_id: int, title: str) -> None:
    try:
        with service.transaction() as conn:
            service.move_back_to_inbox(conn, source_id)
        st.session_state.done_flash = ("success", f"Moved back to Inbox: {title}")
    except Exception as exc:
        st.session_state.done_flash = ("error", f"Could not move it back: {exc}")


with service.transaction() as conn:
    action_types = service.done_action_types(conn)

action = st.selectbox("Action", ["All"] + action_types,
                      format_func=lambda a: "All actions" if a == "All" else action_label(a))

if st.session_state.done_flash:
    kind, msg = st.session_state.done_flash
    getattr(st, kind)(msg.replace("$", r"\$"))  # "$" pairs would render as LaTeX
    st.session_state.done_flash = None

with service.transaction() as conn:
    rows = service.list_done(conn, limit=300, action=None if action == "All" else action)

mine = [r for r in rows if not r["automatic"]]
auto = [r for r in rows if r["automatic"]]


def _when(ts: int) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d:%a} {d:%b} {d.day}, {d:%H:%M}"


def render(r: dict) -> None:
    with st.container(border=True, key=f"row_{r['id']}"):
        where = f" → {md(r['destination'])}" if r["destination"] else ""
        st.caption(f"{_when(r['created_at'])} · **{action_label(r['action'])}**{where}")
        st.markdown(f"**{md(r['display_title'])}**")
        if r["note"]:
            st.write(md(r["note"]))
        st.markdown(f"[Open original]({r['canonical_url']})")
        if r["undone_at"]:
            st.caption(f"Moved back to Inbox {_when(r['undone_at'])}")
        elif r["post_status"] and r["post_status"] != "open":
            st.button("Move back to Inbox", key=f"back_{r['id']}", on_click=do_move_back,
                      args=(r["source_id"], r["display_title"]))


st.caption(f"{len(mine)} by you · {len(auto)} automatic")
if not mine:
    st.info("Nothing here yet. Actions you take in the Inbox show up here.")
for r in mine:
    render(r)

if auto:
    with st.expander(f"Done automatically ({len(auto)})", expanded=False):
        for r in auto:
            render(r)
