"""Sprint 3 tests: resume loading, Prep Notebook, Jobs. Temp database, fake LLMs, no API calls."""
import json
import os

import pytest

from test_copilot import DAY, NOW, conn, fake_llm_factory  # noqa: F401  (conn is a fixture)

RESUME = "## Experience\n- Led AI platform launches at a large retailer\n- Ran incident reviews for 40 teams"


# --- fakes -------------------------------------------------------------------------

class FakeLLM:
    """Answers prep and fit prompts; counts calls and records what was sent."""

    def __init__(self, topics=("Incident management and postmortems",), fit_score=82, employer="Acme Cloud"):
        self.calls, self.topics, self.fit_score, self.employer = [], list(topics), fit_score, employer

    def __call__(self, system, user):
        self.calls.append((system, user))
        if "interview prep notes" in system:
            body = {"topics": self.topics, "notes": ["Blameless reviews find causes", "Track action items"],
                    "use_when": "Use it in an answer when asked about a failed launch"}
        else:
            body = {"fit_score": self.fit_score, "reasons": ["Led platform launches", "Incident work"],
                    "employer": self.employer, "concern": "No people-management evidence"}
        return json.dumps(body), {"cost": 0.0005}


def _triaged(conn):
    from contentpilot.copilot import service
    service.backfill_facts(conn)
    service.run_triage(conn, llm=fake_llm_factory([]), now=NOW)
    conn.commit()


def _resume(text=RESUME):
    from contentpilot.copilot.resume import Resume
    return Resume(text, "resume.md", None)


# --- 0. resume loading -----------------------------------------------------------------

def _make_docx(path, heading="Experience", bullet="Led AI platform launches"):
    import docx
    d = docx.Document()
    d.add_heading("Jane Candidate", level=1)
    p = d.add_paragraph()
    p.add_run(heading).bold = True
    d.add_paragraph(bullet, style="List Bullet")
    d.add_paragraph("Senior TPM, 2020-2026.")
    d.save(path)


def test_resume_md_wins_over_docx(tmp_path):
    from contentpilot.copilot import service
    _make_docx(tmp_path / "cv.docx")
    (tmp_path / "resume.md").write_text(RESUME, encoding="utf-8")
    r = service.load_resume(str(tmp_path))
    assert r.ok and r.source == "resume.md" and r.text == RESUME


def test_resume_newest_docx_keeps_headings_and_bullets(tmp_path):
    from contentpilot.copilot import service
    _make_docx(tmp_path / "old.docx", bullet="Old bullet")
    _make_docx(tmp_path / "new.docx", bullet="New bullet")
    os.utime(tmp_path / "old.docx", (1_000_000, 1_000_000))
    (tmp_path / "~$new.docx").write_bytes(b"lock file")           # Word lock file is ignored
    r = service.load_resume(str(tmp_path))
    assert r.source == "new.docx"
    lines = r.text.splitlines()
    assert "# Jane Candidate" in lines and "## Experience" in lines and "- New bullet" in lines
    assert "Senior TPM, 2020-2026." in lines


def test_resume_old_doc_only_gives_notice_and_skips_scoring(tmp_path, conn):
    from contentpilot.copilot import service
    (tmp_path / "resume.doc").write_bytes(b"\xd0\xcf\x11\xe0")
    r = service.load_resume(str(tmp_path))
    assert not r.ok and r.notice == "Save your resume as .docx in Word"
    _triaged(conn)
    llm = FakeLLM()
    stats = service.score_jobs(conn, r, llm=llm, now=NOW)
    assert stats["skipped"] == "Save your resume as .docx in Word" and llm.calls == []


def test_resume_missing(tmp_path):
    from contentpilot.copilot import service
    assert not service.load_resume(str(tmp_path)).ok


# --- A. Prep Notebook ----------------------------------------------------------------------

def test_seed_prep_topics_once(conn):
    from contentpilot.copilot import service
    assert service.seed_prep_topics(conn) == 22
    assert service.seed_prep_topics(conn) == 0
    names = [t["name"] for t in service.list_prep_topics(conn)]
    assert names[0] == "Amazon Leadership Principles" and names[-2:] == ["Energy & Shell domain", "AI and ML for TPMs"]


def test_file_to_prep_one_cached_call(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    llm = FakeLLM(topics=["incident management and postmortems", "Risk management and mitigation planning"])
    res = service.file_to_prep(conn, 3, llm=llm, now=NOW)
    assert res["llm_calls"] == 1 and len(llm.calls) == 1
    assert res["topics"] == ["Incident management and postmortems", "Risk management and mitigation planning"]
    assert [t["name"] for t in service.prep_topics_for_source(conn, 3)] == res["topics"]
    note = service.list_prep_notes(conn, service.prep_topics_for_source(conn, 3)[0]["id"])[0]
    assert "- Blameless reviews find causes" in note["text"] and "Use it in an answer when" in note["text"]
    assert note["canonical_url"] == "https://li/3"
    assert conn.execute("SELECT status FROM post_state WHERE source_id = 3").fetchone()["status"] == "done"
    assert service.list_done(conn)[0]["destination"].startswith("Prep: Incident management")

    # Moved back and filed again: no new call and no duplicate notes.
    service.move_back_to_inbox(conn, 3, now=NOW + 1)
    assert service.file_to_prep(conn, 3, llm=llm, now=NOW + 2)["llm_calls"] == 0 and len(llm.calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM prep_notes WHERE source_id = 3").fetchone()[0] == 2

    # Notes lost but the cache kept: still no call.
    conn.execute("DELETE FROM prep_notes WHERE source_id = 3")
    assert service.file_to_prep(conn, 3, llm=llm, now=NOW + 3)["llm_calls"] == 0 and len(llm.calls) == 1


def test_file_to_prep_rejects_unknown_topic_then_retries(conn):
    from contentpilot.copilot import service
    from contentpilot.copilot.llmjson import LLMJsonError
    _triaged(conn)
    llm = FakeLLM(topics=["Underwater basket weaving"])
    with pytest.raises(LLMJsonError):
        service.file_to_prep(conn, 3, llm=llm, now=NOW)
    assert len(llm.calls) == 2                                   # one retry, then give up
    assert conn.execute("SELECT COUNT(*) FROM prep_notes").fetchone()[0] == 0


def test_change_topic_moves_notes_without_a_call(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    llm = FakeLLM()
    service.file_to_prep(conn, 3, llm=llm, now=NOW)
    ids = {t["name"]: t["id"] for t in service.list_prep_topics(conn)}
    service.move_prep_notes(conn, 3, ids["Incident management and postmortems"], ids["AI and ML for TPMs"])
    assert [t["name"] for t in service.prep_topics_for_source(conn, 3)] == ["AI and ML for TPMs"]
    assert service.list_prep_notes(conn, ids["Incident management and postmortems"]) == []
    assert len(llm.calls) == 1
    assert service.list_done(conn)[0]["destination"] == "Prep: AI and ML for TPMs"


def test_file_all_learning_and_coverage(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    conn.execute("UPDATE post_state SET post_type = 'learning' WHERE source_id IN (5, 6)")  # 3 learning posts
    llm = FakeLLM()
    stats = service.file_all_learning(conn, llm=llm, now=NOW, daily_cap_usd=1.0)
    assert stats["total"] == 3 and stats["filed"] == 3 and stats["llm_calls"] == 3
    assert service.open_learning_ids(conn, NOW) == []
    cov = service.prep_coverage(conn)
    assert cov == {"covered": 1, "total": 22, "pct": 5}            # 3 notes in one topic


def test_file_all_learning_stops_at_cap(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    llm = FakeLLM()
    stats = service.file_all_learning(conn, llm=llm, now=NOW, daily_cap_usd=0.0)
    assert stats["stopped_by_cap"] and llm.calls == []


def test_my_stories(conn):
    from contentpilot.copilot import service
    service.seed_prep_topics(conn)
    tid = service.list_prep_topics(conn)[0]["id"]
    service.add_story(conn, tid, "S: outage. T: restore. A: rollback. R: 20 min MTTR.", now=NOW)
    service.add_story(conn, tid, "Second story", now=NOW + 5)
    notes = service.list_prep_notes(conn, tid)
    assert [n["text"] for n in notes] == ["Second story", "S: outage. T: restore. A: rollback. R: 20 min MTTR."]
    assert notes[0]["source_id"] is None
    topic = service.list_prep_topics(conn)[0]
    assert topic["note_count"] == 2 and topic["story_count"] == 2
    with pytest.raises(ValueError):
        service.add_story(conn, tid, "   ")


class _Answer:
    def __init__(self, text):
        self.text = text

    def model_dump(self):
        return {"answer": self.text, "key_points": ["k"], "follow_up_questions": ["f"], "sources_used": [],
                "confidence": "low", "gaps": []}


def test_saved_answers_reopen_with_zero_calls(conn):
    from contentpilot.copilot import service
    service.seed_prep_topics(conn)
    tid = service.list_prep_topics(conn)[7]["id"]
    calls = {"retrieve": 0, "synth": 0}

    def retrieve(q):
        calls["retrieve"] += 1
        return ["chunk"]

    def synth(q, chunks):
        calls["synth"] += 1
        return _Answer(f"answer {calls['synth']}")

    first = service.ask_prep(conn, "  How do you run a Postmortem? ", tid, retrieve, synth, now=NOW)
    assert not first["cached"] and first["answer"]["answer"] == "answer 1"
    again = service.ask_prep(conn, "how do you run a postmortem?", None, retrieve, synth, now=NOW + 1)
    assert again["cached"] and again["answer"]["answer"] == "answer 1" and calls == {"retrieve": 1, "synth": 1}

    saved = service.list_prep_answers(conn, tid)
    assert [a["question"] for a in saved] == ["How do you run a Postmortem?"]
    assert service.list_prep_topics(conn)[7]["answer_count"] == 1

    regen = service.ask_prep(conn, "How do you run a Postmortem?", None, retrieve, synth, regenerate=True, now=NOW + 2)
    assert not regen["cached"] and regen["answer"]["answer"] == "answer 2" and calls["synth"] == 2
    assert regen["topic_id"] == tid                                # stays under its topic
    assert len(service.list_prep_answers(conn, tid)) == 1           # replaced, not duplicated


def test_ask_with_empty_library_saves_nothing(conn):
    from contentpilot.copilot import service
    assert service.ask_prep(conn, "anything", 1, lambda q: [], lambda q, c: _Answer("x"), now=NOW) is None
    assert conn.execute("SELECT COUNT(*) FROM outputs WHERE kind = 'prep_answer'").fetchone()[0] == 0


# --- B. Jobs ------------------------------------------------------------------------------

def test_list_jobs_window_and_states(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    # Post 1: open job published 5 days before NOW. Post 2: archived (old). Add two more jobs.
    conn.execute("UPDATE post_state SET post_type = 'job' WHERE source_id IN (3, 5)")
    conn.execute("UPDATE sources SET published_at = ? WHERE id = 3", (NOW - 70 * DAY,))  # too old to rank
    conn.execute("UPDATE sources SET published_at = NULL WHERE id = 5")                   # undated: included
    assert [j["source_id"] for j in service.list_jobs(conn, None, NOW)] == [1, 5]
    service.record_action(conn, 1, "shortlist", now=NOW)                                  # shortlisted stays
    service.set_job_status(conn, 5, "not_a_fit", now=NOW)                                 # not a fit drops
    assert [j["source_id"] for j in service.list_jobs(conn, None, NOW)] == [1]
    assert service.jobs_archived_count(conn) == 1


def test_fit_scores_cached_per_resume_version(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    conn.execute("UPDATE post_state SET post_type = 'job' WHERE source_id = 5")
    conn.execute("UPDATE sources SET published_at = NULL WHERE id = 5")
    llm = FakeLLM()
    r1 = _resume()
    assert service.unscored_jobs_count(conn, r1.hash, NOW) == 2
    assert service.score_jobs(conn, r1, llm=llm, now=NOW, daily_cap_usd=1.0)["llm_calls"] == 2
    assert RESUME in llm.calls[0][1]                                 # the resume goes to the fit call only
    assert service.score_jobs(conn, r1, llm=llm, now=NOW, daily_cap_usd=1.0)["llm_calls"] == 0  # unchanged resume
    jobs = service.list_jobs(conn, r1.hash, NOW)
    assert all(j["fit"]["fit_score"] == 82 for j in jobs)
    stored = conn.execute("SELECT content_json FROM outputs WHERE kind = 'fit'").fetchall()
    assert all("Led AI platform launches at a large retailer" not in r["content_json"] for r in stored)

    r2 = _resume(RESUME + "\n- Added a new bullet")                  # edited resume: re-score once
    assert service.unscored_jobs_count(conn, r2.hash, NOW) == 2
    assert service.score_jobs(conn, r2, llm=llm, now=NOW, daily_cap_usd=1.0)["llm_calls"] == 2
    assert service.score_jobs(conn, r2, llm=llm, now=NOW, daily_cap_usd=1.0)["llm_calls"] == 0
    assert len(llm.calls) == 4


def test_ranking_named_employer_first_and_top3(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    conn.execute("UPDATE post_state SET post_type = 'job', status = 'open' WHERE source_id IN (3, 4, 5, 6)")
    conn.execute("UPDATE sources SET published_at = NULL WHERE id IN (3, 4, 5, 6)")
    r = _resume()
    scores = {1: (70, "Acme"), 3: (95, None), 4: (60, "Beta"), 5: (88, "Gamma"), 6: (40, "Delta")}
    for sid, (score, employer) in scores.items():
        service._store_output(conn, "fit", f"{sid}:{r.hash}", "fit_v1",
                              {"fit_score": score, "reasons": ["r"], "employer": employer, "concern": "c"},
                              None, 0.0, NOW)
    jobs = service.list_jobs(conn, r.hash, NOW)
    assert [j["source_id"] for j in jobs] == [5, 1, 4, 6, 3]         # no employer (95) ranks last
    assert [j["source_id"] for j in jobs if j["apply_this_week"]] == [5, 1, 4]
    service.set_job_status(conn, 1, "applied", now=NOW)               # applied: still listed, not flagged
    jobs = service.list_jobs(conn, r.hash, NOW)
    assert 1 in [j["source_id"] for j in jobs]
    assert [j["source_id"] for j in jobs if j["apply_this_week"]] == [5, 4, 6]


def test_apply_pack_marks_copied_not_applied(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    pack = service.show_apply_pack(conn, 1, _resume(), now=NOW)
    assert pack.index("=== JOB POST ===") < pack.index("=== MY RESUME ===") < pack.index("INSTRUCTIONS")
    assert "We are hiring a Senior TPM for AI platform." in pack and RESUME in pack
    for phrase in ("2 pages", "Keep every fact true", "missing", "cover note", "3 questions"):
        assert phrase in pack
    jt = conn.execute("SELECT user_status, pack_copied_at FROM job_tracking WHERE source_id = 1").fetchone()
    assert jt["user_status"] == "not_started" and jt["pack_copied_at"] == NOW
    assert conn.execute("SELECT status FROM post_state WHERE source_id = 1").fetchone()["status"] == "open"
    assert service.list_jobs(conn, None, NOW)[0]["user_status"] == "not_started"


def test_deferred_job_status_with_date(conn):
    from contentpilot.copilot import service
    _triaged(conn)
    service.set_job_status(conn, 1, "deferred", note="after re:Invent", deferred_until=NOW + 10 * DAY, now=NOW)
    job = service.list_jobs(conn, None, NOW)[0]
    assert job["user_status"] == "deferred" and job["deferred_until"] == NOW + 10 * DAY and job["job_note"] == "after re:Invent"


def test_llm_json_with_literal_newlines_in_strings_parses():
    """Models put raw newlines inside multi-paragraph strings; strict JSON rejected them (seen live)."""
    from contentpilot.copilot.llmjson import call_json
    from contentpilot.copilot.prep import PrepNote
    from contentpilot.rag.synthesizer import _parse

    raw = '{\n "answer": "First paragraph.\n\nSecond paragraph.",\n "key_points": ["a"],\n' \
          ' "follow_up_questions": ["q"], "sources_used": [], "confidence": "low", "gaps": []}'
    assert _parse(raw).answer == "First paragraph.\n\nSecond paragraph."

    calls = []

    def llm(system, user):
        calls.append(1)
        return '{"topics": ["AI and ML for TPMs"], "notes": ["one\nline", "two"], "use_when": "Use it in an answer when x"}', {}
    note, _ = call_json(llm, "s", "u", PrepNote)
    assert note.notes[0] == "one\nline" and len(calls) == 1          # no retry needed
