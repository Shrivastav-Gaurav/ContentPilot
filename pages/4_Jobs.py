"""Jobs: recent job posts ranked by fit against your resume, with Apply packs and status.

Loading this page makes no LLM call. Fit scores come from the cache; "Score" is the
only button that calls the model (one call per job, once per resume version).
"""
import time
from datetime import date, datetime, timedelta

import streamlit as st
from dotenv import load_dotenv

from contentpilot.copilot import service
from contentpilot.copilot.display import due_badge, fmt_date, md

load_dotenv()

st.set_page_config(page_title="Jobs — ContentPilot", page_icon="💼")
st.title("Jobs")

STATUS_KEYS = list(service.JOB_STATUS_LABELS)
st.session_state.setdefault("jobs_flash", None)
st.session_state.setdefault("packs", {})   # source_id -> pack text shown this session


def _flash(kind, text):
    st.session_state.jobs_flash = (kind, text)


resume = service.load_resume()   # reads a local file; the text is never logged


def score_all() -> None:
    try:
        with service.transaction() as conn:
            stats = service.score_jobs(conn, resume)
    except Exception as exc:
        _flash("error", f"Scoring failed: {exc}")
        return
    msg = f"Scored {stats['scored']} job{'s' if stats['scored'] != 1 else ''} (${stats['cost_usd']:.3f})."
    if stats["stopped_by_cap"]:
        msg += " Stopped at today's spend cap; the rest can be scored tomorrow."
    if stats["errors"]:
        msg += f" {stats['errors']} failed; press Score again to retry them."
    _flash("warning" if stats["stopped_by_cap"] or stats["errors"] else "success", msg)


def show_pack(source_id: int) -> None:
    try:
        with service.transaction() as conn:
            st.session_state.packs[source_id] = service.show_apply_pack(conn, source_id, resume)
    except Exception as exc:
        _flash("error", f"Could not build the Apply pack: {exc}")


def hide_pack(source_id: int) -> None:
    st.session_state.packs.pop(source_id, None)


def save_status(source_id: int) -> None:
    status = st.session_state[f"status_{source_id}"]
    note = (st.session_state.get(f"note_{source_id}") or "").strip() or None
    deferred_until = None
    if status == "deferred":
        d = st.session_state.get(f"defer_{source_id}") or (date.today() + timedelta(days=7))
        deferred_until = int(datetime(d.year, d.month, d.day, 9, 0).timestamp())
    try:
        with service.transaction() as conn:
            service.set_job_status(conn, source_id, status, note=note, deferred_until=deferred_until)
        _flash("success", f"Saved: {service.JOB_STATUS_LABELS[status]}.")
    except Exception as exc:
        _flash("error", f"Could not save the status: {exc}")


now = int(time.time())
with service.transaction() as conn:
    jobs = service.list_jobs(conn, resume.hash, now)
    archived = service.jobs_archived_count(conn)

if not resume.ok:
    st.warning(f"{resume.notice} Fit scores and Apply packs need it.")
else:
    st.caption(f"Resume: {resume.source}")

if st.session_state.jobs_flash:
    kind, text = st.session_state.jobs_flash
    getattr(st, kind)(text)
    st.session_state.jobs_flash = None

unscored = sum(1 for j in jobs if j["fit"] is None)
st.caption(f"{len(jobs)} job{'s' if len(jobs) != 1 else ''} published in the last 60 days or undated · "
           f"{archived} archived as too old")
if resume.ok and unscored:
    st.button(f"Score {unscored} job{'s' if unscored != 1 else ''} against my resume (one call each)",
              type="primary", on_click=score_all)

if not jobs:
    st.info("No open or shortlisted jobs. Job posts from your Inbox show up here.")
    st.stop()

table = [{
    "#": j["rank"],
    "Fit": j["fit"]["fit_score"] if j["fit"] else None,
    "Role": j["role_title"] or j["display_title"],
    "Employer": (j["fit"] or {}).get("employer") or ("Check employer" if j["fit"] else "—"),
    "Status": service.JOB_STATUS_LABELS[j["user_status"]],
    "": "Apply this week" if j["apply_this_week"] else "",
} for j in jobs]
st.dataframe(table, hide_index=True, width="stretch",
             column_config={"#": st.column_config.NumberColumn(width="small"),
                            "Fit": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d")})

for j in jobs:
    sid = j["source_id"]
    f = j["fit"]
    with st.container(border=True, key=f"row_{sid}"):
        badges = []
        if j["apply_this_week"]:
            badges.append(":green-badge[Apply this week]")
        if f:
            badges.append(f":blue-badge[Fit {f['fit_score']}]")
        else:
            badges.append(":gray-badge[Not scored]")
        if f and not f.get("employer"):
            badges.append(":orange-badge[Check employer]")
        if j["shortlisted"]:
            badges.append(":violet-badge[Shortlisted]")
        if j["status"] == "open" and j["user_status"] != "deferred":
            badges.append(due_badge(j["due_at"], now))
        st.markdown(" ".join(b for b in badges if b))
        st.markdown(f"**{j['rank']}. {md(j['role_title'] or j['display_title'])}**")
        employer = (f or {}).get("employer")
        st.caption(" · ".join(x for x in (
            md(employer) if employer else None,
            f"posted by {md(j['publisher'])}" if j["publisher"] else None,
            fmt_date(j["published_at"]),
        ) if x))
        if f:
            st.markdown("\n".join(f"- {md(r)}" for r in f["reasons"]))
            if f.get("concern"):
                st.markdown(f"⚠️ {md(f['concern'])}")
            if not employer:
                st.caption("The post doesn't name the employer. Check it before you spend time on this one.")
        elif j["summary"]:
            st.write(md(j["summary"]))
        st.markdown(f"[Open original]({j['canonical_url']})")

        if sid in st.session_state.packs:
            st.caption("Copy with the icon at the top right of the box, then paste into a chat.")
            st.code(st.session_state.packs[sid], language=None, wrap_lines=True, height=320)
            st.button("Hide Apply pack", key=f"hide_{sid}", on_click=hide_pack, args=(sid,))
        else:
            st.button("Show Apply pack", key=f"pack_{sid}", on_click=show_pack, args=(sid,), disabled=not resume.ok,
                      help=None if resume.ok else resume.notice)
        if j["pack_copied_at"]:
            st.caption(f"Apply pack last shown {fmt_date(j['pack_copied_at'])}")

        current = j["user_status"]
        st.selectbox("Status", STATUS_KEYS, index=STATUS_KEYS.index(current), key=f"status_{sid}",
                     format_func=service.JOB_STATUS_LABELS.get)
        st.text_input("Note (optional)", value=j["job_note"] or "", key=f"note_{sid}")
        if st.session_state.get(f"status_{sid}") == "deferred":
            default = (datetime.fromtimestamp(j["deferred_until"]).date() if j["deferred_until"]
                       else date.today() + timedelta(days=7))
            st.date_input("Come back to it on", value=default, min_value=date.today(), key=f"defer_{sid}")
        st.button("Save", key=f"save_{sid}", on_click=save_status, args=(sid,))
