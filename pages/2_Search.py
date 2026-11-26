"""Search every saved post, any status. Keyword search makes no API calls; "Search by meaning"
embeds the query once (cached) using the stored post vectors."""
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

with service.transaction() as conn:
    meaning_ready = search.meaning_search_ready(conn)
    min_match = search.get_min_match(conn)
by_meaning = st.toggle(
    "Search by meaning", key="by_meaning", disabled=not meaning_ready,
    help=("Finds posts about the same idea even when the words differ. One tiny embedding call per new search."
          if meaning_ready else
          "Needs OPENAI_API_KEY in .env and stored vectors: run python -m scripts.embed_backfill once."))

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
    getattr(st, kind)(msg.replace("$", r"\$"))  # "$" pairs would render as LaTeX
    st.session_state.search_flash = None

meaning = by_meaning and meaning_ready and bool(text.strip())
try:
    with service.transaction() as conn:
        results = search.search(
            conn, text=text, status=STATUSES[status], post_type=TYPES[post_type],
            published_since=since, source_type=None if source == "Any" else source, limit=50,
            by_meaning=meaning,
        )
except Exception as exc:  # cap reached or the embeddings API failed: fall back to keywords
    reason = str(exc) if isinstance(exc, service.DailyCapReached) else f"The embeddings call failed ({type(exc).__name__})."
    st.warning(f"{reason} Showing keyword results instead.".replace("$", r"\$"))
    meaning = False
    with service.transaction() as conn:
        results = search.search(
            conn, text=text, status=STATUSES[status], post_type=TYPES[post_type],
            published_since=since, source_type=None if source == "Any" else source, limit=50,
        )

filtered = bool(text.strip()) or any(v != "Any" for v in (status, post_type, published, source))
st.caption(f"{len(results)} result{'s' if len(results) != 1 else ''}"
           + (f" · {min_match:.0%}+ match, closest first" if meaning else "") + ("" if filtered else " · latest saved posts")
           + (" (showing the first 50)" if len(results) == 50 else ""))
if not results and meaning:
    st.info("No close matches - try keywords")
elif not results:
    st.info("No posts match. Try fewer words or set the filters to Any.")

for r in results:
    with st.container(border=True, key=f"row_{r['id']}"):
        st.markdown(f"{type_badge(r['post_type'])} {status_badge(r['status'])}")
        st.markdown(f"**{md(r['display_title'])}**")
        match = f" · {r['similarity']:.0%} match" if "similarity" in r else ""
        st.caption(f"{md(r['publisher'] or 'Unknown publisher')} · {fmt_date(r['published_at'])} · {r['source_type']}{match}")
        if r["summary"]:
            st.write(md(r["summary"]))
        st.markdown(f"[Open original]({r['canonical_url']})")
        if r["status"] != "open" and r["post_type"]:
            st.button("Move to Inbox", key=f"mv_{r['id']}", on_click=do_move_to_inbox,
                      args=(r["id"], r["display_title"]))
