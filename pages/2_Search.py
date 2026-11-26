"""Search every saved post, any status. No API calls."""
import time

import streamlit as st
from dotenv import load_dotenv

from contentpilot.copilot import rules, search, service
from contentpilot.copilot.display import fmt_date, md, status_badge, type_badge
from contentpilot.copilot.rules import TYPE_LABELS

load_dotenv()

st.set_page_config(page_title="Search — ContentPilot", page_icon="🔎")
st.title("Search")

st.session_state.setdefault("search_flash", None)


def do_move_to_inbox(source_id: int, title: str) -> None:
    try:
        with service.transaction() as conn:
            service.move_back_to_inbox(conn, source_id)
        st.session_state.search_flash = ("success", f"Moved to Inbox: {title}")
    except Exception as exc:
        st.session_state.search_flash = ("error", f"Could not move it: {exc}")


text = st.text_input("Search", placeholder="keyword, phrase or publisher", label_visibility="collapsed")

STATUSES = {"Any": None, "Open": "open", "Actioned": "actioned", "Archived": "archived", "Dismissed": "dismissed"}
TYPES = {"Any": None, **{label: key for key, label in TYPE_LABELS.items()}}
PUBLISHED = ("Any", "Last 30 days", "Last 90 days", "This year")

with service.transaction() as conn:
    source_options = ["Any"] + search.source_types(conn)

c1, c2 = st.columns(2)
status = c1.selectbox("Status", list(STATUSES))
post_type = c2.selectbox("Type", list(TYPES))
c3, c4 = st.columns(2)
published = c3.selectbox("Published", PUBLISHED)
source = c4.selectbox("Source", source_options, format_func=lambda s: s if s == "Any" else s.capitalize())

now = int(time.time())
since = {
    "Last 30 days": now - 30 * rules.DAY,
    "Last 90 days": now - 90 * rules.DAY,
    "This year": rules.start_of_year(now),
}.get(published)

if st.session_state.search_flash:
    kind, msg = st.session_state.search_flash
    getattr(st, kind)(msg)
    st.session_state.search_flash = None

with service.transaction() as conn:
    results = search.search(
        conn, text=text, status=STATUSES[status], post_type=TYPES[post_type],
        published_since=since, source_type=None if source == "Any" else source, limit=50,
    )

filtered = bool(text.strip()) or any(v != "Any" for v in (status, post_type, published, source))
st.caption(f"{len(results)} result{'s' if len(results) != 1 else ''}" + ("" if filtered else " · latest saved posts")
           + (" (showing the first 50)" if len(results) == 50 else ""))
if not results:
    st.info("No posts match. Try fewer words or set the filters to Any.")

for r in results:
    with st.container(border=True, key=f"row_{r['id']}"):
        st.markdown(f"{type_badge(r['post_type'])} {status_badge(r['status'])}")
        st.markdown(f"**{md(r['display_title'])}**")
        st.caption(f"{md(r['publisher'] or 'Unknown publisher')} · {fmt_date(r['published_at'])} · {r['source_type']}")
        if r["summary"]:
            st.write(md(r["summary"]))
        st.markdown(f"[Open original]({r['canonical_url']})")
        if r["status"] != "open" and r["post_type"]:
            st.button("Move to Inbox", key=f"mv_{r['id']}", on_click=do_move_to_inbox,
                      args=(r["id"], r["display_title"]))
