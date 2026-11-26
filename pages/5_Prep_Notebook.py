"""Prep Notebook: interview topics, notes filed from posts, your own stories, saved answers.

Loading this page makes no LLM call and never touches Chroma. Only "Ask" (first time for a
question), "Regenerate" (Haiku) and "Deeper answer (Sonnet)" call the retriever and synthesizer.
"""
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

from contentpilot.copilot import service
from contentpilot.copilot.display import fmt_date, md

load_dotenv()

st.set_page_config(page_title="Prep Notebook — ContentPilot", page_icon="🎯")

CONFIDENCE = {"high": "🟢", "medium": "🟡", "low": "🔴"}

st.session_state.setdefault("prep_flash", None)
st.session_state.setdefault("prep_open_answer", None)


def _flash(kind, text):
    st.session_state.prep_flash = (kind, text)


def open_topic(topic_id: int | None) -> None:
    if topic_id is None:
        st.query_params.clear()
    else:
        st.query_params["topic"] = str(topic_id)
    st.session_state.prep_open_answer = None


def save_story(topic_id: int, widget_key: str) -> None:
    text = st.session_state.get(widget_key, "")
    try:
        with service.transaction() as conn:
            service.add_story(conn, topic_id, text)
        st.session_state[widget_key] = ""
        _flash("success", "Story saved.")
    except ValueError:
        _flash("warning", "Write the story first.")
    except Exception as exc:
        _flash("error", f"Could not save it: {exc}")


def ask(question: str, topic_id: int, regenerate: bool = False, deep: bool = False) -> None:
    if not question.strip():
        _flash("warning", "Type a question first.")
        return
    try:
        with st.spinner("Searching your library and writing an answer…"):
            with service.transaction() as conn:
                res = service.ask_prep(conn, question, topic_id, regenerate=regenerate or deep, deep=deep)
    except service.DailyCapReached as exc:
        _flash("warning", f"{exc} No call was made. Saved answers still open; try again tomorrow.")
        return
    except Exception as exc:
        _flash("error", f"Could not answer: {exc}")
        return
    if res is None:
        _flash("warning", "Nothing in your library matches that question yet. File more posts, then ask again.")
        return
    st.session_state.prep_open_answer = service.question_key(question)
    if res["cached"]:
        _flash("info", "You asked this before. Showing the saved answer (no new call). Use Regenerate for a fresh one.")
    elif deep:
        _flash("success", "Deeper answer (Sonnet) saved in place of the previous one.")
    elif regenerate:
        _flash("success", "Answer regenerated and saved.")
    else:
        _flash("success", "Answer saved under this topic.")


def render_answer(a: dict) -> None:
    ans = a["answer"]
    asked = datetime.fromtimestamp(a.get("asked_at") or a.get("saved_at") or 0)
    model = (a.get("model") or "").split("/")[-1]
    st.caption(f"{CONFIDENCE.get(ans.get('confidence'), '⚪')} Confidence: {ans.get('confidence', '?')} · "
               f"saved {asked:%b} {asked.day}" + (f" · {model}" if model else ""))
    st.markdown(ans["answer"].replace("$", r"\$"))
    if ans.get("key_points"):
        st.markdown("**Key points**")
        st.markdown("\n".join(f"- {md(p)}" for p in ans["key_points"]))
    if ans.get("gaps"):
        st.markdown("**Gaps in your library**")
        st.markdown("\n".join(f"- {md(g)}" for g in ans["gaps"]))
    if ans.get("sources_used"):
        st.markdown("**Sources**")
        st.markdown("\n".join(f"- {md(s['author'])}: [{md(s['title'])}]({s['url']})" for s in ans["sources_used"]))
    if ans.get("follow_up_questions"):
        st.markdown("**Follow-up questions to practise**")
        st.markdown("\n".join(f"- {md(q)}" for q in ans["follow_up_questions"]))


# --- data (no LLM, no Chroma) -------------------------------------------------------------

topic_param = st.query_params.get("topic")
with service.transaction() as conn:
    service.seed_prep_topics(conn)
    topics = service.list_prep_topics(conn)
    coverage = service.prep_coverage(conn)
    topic = service.get_prep_topic(conn, int(topic_param)) if topic_param and topic_param.isdigit() else None
    notes = service.list_prep_notes(conn, topic["id"]) if topic else []
    answers = service.list_prep_answers(conn, topic["id"]) if topic else []

if st.session_state.prep_flash:
    kind, text = st.session_state.prep_flash
    getattr(st, kind)(text.replace("$", r"\$"))  # "$0.10 … $0.25" would render as LaTeX
    st.session_state.prep_flash = None

# --- topic list -----------------------------------------------------------------------------

if topic is None:
    st.title("Prep Notebook")
    st.caption("Notes from posts you filed, your own stories, and saved answers, by interview topic.")
    st.metric("Topics covered", f"{coverage['covered']} of {coverage['total']}",
              help="A topic counts as covered once it has 2 or more notes.")
    st.progress(coverage["pct"] / 100)
    for t in topics:
        parts = [f"{t['note_count']} note{'s' if t['note_count'] != 1 else ''}"]
        if t["answer_count"]:
            parts.append(f"{t['answer_count']} answer{'s' if t['answer_count'] != 1 else ''}")
        mark = "✅ " if t["note_count"] >= 2 else ""
        st.button(f"{mark}{t['name']} · {', '.join(parts)}", key=f"topic_{t['id']}", on_click=open_topic,
                  args=(t["id"],), width="stretch")
    st.stop()

# --- one topic --------------------------------------------------------------------------------

st.button("← All topics", on_click=open_topic, args=(None,))
st.title(topic["name"])
filed = sum(1 for n in notes if n["source_id"])
stories = len(notes) - filed
st.caption(f"{filed} from posts · {stories} of your {'story' if stories == 1 else 'stories'} · "
           f"{len(answers)} saved answer{'' if len(answers) == 1 else 's'}"
           + (" · covered" if len(notes) >= 2 else " · add 2+ notes to cover this topic"))

st.subheader("Ask a question")
q_key = f"ask_{topic['id']}"
question = st.text_input("Interview question", key=q_key, placeholder="e.g. Tell me about a launch that went wrong",
                         label_visibility="collapsed")
st.button("Ask", type="primary", on_click=lambda: ask(st.session_state.get(q_key, ""), topic["id"]),
          )

if answers:
    st.subheader("Saved answers")
    for a in answers:
        is_open = st.session_state.prep_open_answer == a["key"]
        with st.expander(a["question"], expanded=is_open):
            render_answer(a)
            c1, c2 = st.columns(2)
            c1.button("Regenerate", key=f"regen_{a['key']}", on_click=ask, args=(a["question"], topic["id"], True),
                      help="One new Haiku call; replaces this saved answer.", width="stretch")
            c2.button("Deeper answer (Sonnet)", key=f"deep_{a['key']}", on_click=ask,
                      args=(a["question"], topic["id"], True, True), width="stretch",
                      help="One Sonnet call (several times the cost of Haiku); replaces this saved answer.")

st.subheader("Notes")
if not notes:
    st.info("No notes yet. Use File to Prep on learning posts in the Inbox, or add your own story below.")
for n in notes:
    with st.container(border=True, key=f"note_{n['id']}"):
        if n["source_id"]:
            st.markdown(f"**{md(n['display_title'])}**")
            st.markdown(n["text"].replace("$", r"\$"))
            st.caption(f"{md(n['publisher'] or 'Unknown publisher')} · filed {fmt_date(n['created_at'])}")
            st.markdown(f"[Open original]({n['canonical_url']})")
        else:
            st.markdown(":violet-badge[My story]")
            st.markdown(md(n["text"]).replace("\n", "  \n"))
            st.caption(f"added {fmt_date(n['created_at'])}")

st.subheader("My stories")
story_key = f"story_{topic['id']}"
st.text_area("Add a STAR story for this topic", key=story_key, height=140,
             placeholder="Situation, Task, Action, Result. Numbers help.")
st.button("Save story", on_click=save_story, args=(topic["id"], story_key))
