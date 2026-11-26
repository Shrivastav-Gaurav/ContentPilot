"""Inbox: open posts, soonest due first. Every button writes through the service layer."""
import time

import streamlit as st
from dotenv import load_dotenv

from contentpilot.copilot import service
from contentpilot.copilot.display import action_label, due_badge, fmt_date, md, type_badge
from contentpilot.copilot.rules import TYPE_LABELS
from contentpilot.fetchers.base import FetchBlockedError, FetchUnavailableError

load_dotenv()

st.set_page_config(page_title="Inbox — ContentPilot", page_icon="📥")
st.title("Inbox")

READ = ("Mark read", "read", None)
DISMISS = ("Dismiss", "dismiss", None)
BUTTONS = {
    "job": [("Shortlist", "shortlist", "Job shortlist"), READ, DISMISS],
    "learning": [("File to Prep", "file_to_prep", "Interview Prep"), READ, DISMISS],
    "post_idea": [("Send to PostPilot", "send_postpilot", "PostPilot"),
                  ("Save to notes file", "save_notes", None), READ, DISMISS],
    "tool": [("Park", "park", "Parked tools"), READ, DISMISS],
}
# Post-idea actions that can both happen to one post.
INDEPENDENT = {"send_postpilot", "save_notes"}

st.session_state.setdefault("acted", {})   # source_id -> list of actions done this session
st.session_state.setdefault("flash", None)


def _flash(kind: str, text: str) -> None:
    st.session_state.flash = (kind, text)


def do_action(source_id: int, action: str, destination: str | None) -> None:
    try:
        with service.transaction() as conn:
            if action == "save_notes":
                service.save_to_notes(conn, source_id)
            else:
                service.record_action(conn, source_id, action, destination=destination)
        st.session_state.acted.setdefault(source_id, []).append(action)
    except Exception as exc:
        _flash("error", f"Could not record that: {exc}")


def do_move_back(source_id: int) -> None:
    try:
        with service.transaction() as conn:
            service.move_back_to_inbox(conn, source_id)
        st.session_state.acted.pop(source_id, None)
    except Exception as exc:
        _flash("error", f"Could not move it back: {exc}")


# --- Add a link ------------------------------------------------------------------

with st.form("add_link", clear_on_submit=True):
    url = st.text_input("Add a link", placeholder="https://www.linkedin.com/posts/…")
    intent = st.text_input("What do you want from it?", placeholder="e.g. apply if it's a senior TPM role")
    submitted = st.form_submit_button("Add", type="primary")

if submitted:
    if not url.strip():
        st.warning("Paste a link first.")
    else:
        try:
            with st.spinner("Fetching and sorting…"):
                with service.transaction() as conn:
                    res = service.add_link(conn, url, intent)
            post = res["post"]
            title = md(post["display_title"])
            stats = res["triage"]
            if not res["new"]:
                st.info(f"Already saved: **{title}** ({TYPE_LABELS.get(post['post_type'], 'not sorted')}, "
                        f"{post['status']}). Find it in Search to move it back to the Inbox.")
            elif stats and stats["stopped_by_cap"]:
                st.warning(f"Saved **{title}**, but today's triage budget is used up. It will be sorted on the next run.")
            elif stats and stats["errors"]:
                st.warning(f"Saved **{title}**, but triage failed. It will be retried on the next run.")
            elif post["status"] == "open":
                st.success(f"Added **{title}** as {TYPE_LABELS.get(post['post_type'])}.")
            else:
                st.info(f"Saved **{title}**: sorted as {TYPE_LABELS.get(post['post_type'])} and "
                        f"{post['status']} automatically. It's in the Done log.")
        except FetchBlockedError as exc:
            st.error(f"Couldn't read that page (login wall or sign-up page). {exc}")
        except FetchUnavailableError as exc:
            st.error(f"That page doesn't exist or has no content. {exc}")
        except Exception as exc:
            st.error(f"Something went wrong: {exc}")

with st.expander("Scan LinkedIn Saved"):
    st.caption("Opens a browser, collects your saved posts and queues them for the worker "
               "(`python -m contentpilot.worker`). New posts are sorted into the Inbox when the job finishes.")
    if st.button("Scan LinkedIn Saved Now"):
        try:
            from contentpilot.worker import trigger_saved_scan

            with st.spinner("Opening LinkedIn and scanning saved posts… (a browser window will appear)"):
                job_id = trigger_saved_scan()
            st.success(f"Scan queued as job #{job_id}. Refresh this page after the worker finishes.")
        except Exception as exc:
            st.error(f"Could not start scan: {exc}")

if st.session_state.flash:
    kind, text = st.session_state.flash
    getattr(st, kind)(text)
    st.session_state.flash = None

# --- List --------------------------------------------------------------------------

FILTERS = {"All": None, "Job": "job", "Learning": "learning", "Post idea": "post_idea", "Tool": "tool"}
choice = st.pills("Show", list(FILTERS), default="All", required=True, key="inbox_filter", label_visibility="collapsed")
post_type = FILTERS.get(choice or "All")

now = int(time.time())
with service.transaction() as conn:
    rows = service.list_inbox(conn, now=now, post_type=post_type)
    open_ids = {r["source_id"] for r in rows}
    acted_rows = []
    for sid in list(st.session_state.acted):
        post = service.get_post(conn, sid)
        if post is None or post["status"] == "open":  # moved back elsewhere: show as a normal row
            st.session_state.acted.pop(sid, None)
            continue
        if sid not in open_ids and (post_type is None or post["post_type"] == post_type):
            acted_rows.append(post)

items = sorted(rows + acted_rows, key=lambda r: (r["due_at"] is None, r["due_at"] or 0))
st.caption(f"{len(rows)} open" + (f" · {len(acted_rows)} handled just now" if acted_rows else ""))

if not items:
    st.info("Nothing waiting here. Add a link above, or run a scan.")


def render_row(r: dict) -> None:
    sid = r["source_id"]
    done = st.session_state.acted.get(sid, [])
    with st.container(border=True, key=f"row_{sid}"):
        st.markdown(" ".join(b for b in (type_badge(r["post_type"]), "" if done else due_badge(r["due_at"], now)) if b))
        st.markdown(f"**{md(r['display_title'])}**")
        st.caption(f"{md(r['publisher'] or 'Unknown publisher')} · {fmt_date(r['published_at'])}")
        if r["summary"]:
            st.write(md(r["summary"]))
        if r["suggested_action"]:
            st.markdown(f"➜ _{md(r['suggested_action'])}_")
        st.markdown(f"[Open original]({r['canonical_url']})")

        buttons = BUTTONS.get(r["post_type"], [READ, DISMISS])
        if done:
            st.success(" · ".join(action_label(a) for a in done))
            # A post idea can still take the other independent action.
            extra = [b for b in buttons if b[1] in INDEPENDENT and b[1] not in done] if set(done) <= INDEPENDENT else []
            cols = st.columns(len(extra) + 1)
            for col, (label, action, dest) in zip(cols, extra):
                col.button(label, key=f"{sid}_{action}", on_click=do_action, args=(sid, action, dest),
                           width="stretch")
            cols[-1].button("Move back", key=f"{sid}_back", on_click=do_move_back, args=(sid,),
                            width="stretch")
        else:
            cols = st.columns(len(buttons))
            for col, (label, action, dest) in zip(cols, buttons):
                col.button(label, key=f"{sid}_{action}", on_click=do_action, args=(sid, action, dest),
                           type="primary" if action not in ("read", "dismiss") else "secondary",
                           width="stretch")


for item in items:
    render_row(item)
