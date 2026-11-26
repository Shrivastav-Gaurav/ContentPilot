# ContentPilot v3 — Sprint 1 (data, triage, due rules, search)

Back end only. No screens change yet; Sprint 2 builds Inbox, Search and Done log on top.

## What's new

| File | What it does |
| --- | --- |
| `contentpilot/db.py` | Migration 6: `post_state`, `actions`, `job_tracking`, `outputs`, `prep_topics`, `prep_notes`, `settings`, `posts_fts`, `sources.intent` |
| `contentpilot/copilot/parsing.py` | Publish date, title, publisher from fetched text. No LLM |
| `contentpilot/copilot/rules.py` | Due dates and auto-archive rules (job 3 days / 60-day rank / 90-day archive; learning 7; post idea 14) |
| `contentpilot/copilot/triage.py` | Prompt `triage_v1`, 10 posts per call, cached in `outputs` |
| `contentpilot/copilot/service.py` | Backfill, triage run with daily cap, actions, Move back to Inbox, user-set job status, Inbox and Done log queries |
| `contentpilot/copilot/search.py` | Full-text search over post text, title, publisher, topics and your notes, any status |
| `scripts/v3_backfill.py` | One command to upgrade the existing database |
| `tests/test_copilot.py` | 6 tests, fake LLM, temp database |

## Apply and run (in VS Code terminal, project folder)

```
git checkout -b v3-sprint1
git apply contentpilot-v3-sprint1.patch
python -m pytest tests -q                 # expect: 6 passed
python -m scripts.v3_backfill --dry-run   # no API calls; shows what will happen
python -m scripts.v3_backfill             # backs up the DB first, then ~11 Haiku calls, about $0.02
```

The dry run applies the new tables (empty) but rolls back every data change.

## Checked against a copy of your database (fake LLM)

- Migration 6 applies cleanly over versions 1–5.
- 68 publish dates and 5 publisher names recovered from text; 35 posts still have no date (YouTube, some articles).
- 108 posts triaged in 11 batched calls; a second run made 0 calls (all cached).
- Search by publisher, keyword and status works, archived and dismissed included.
- No backlog post starts overdue: due dates start from today.

## Rules this sprint guarantees

- Copying an Apply pack never changes a job's status; only `set_job_status` does.
- A post can carry several actions (e.g. sent to PostPilot and saved to notes).
- Move back to Inbox keeps the history and gives a fresh due date.
- Nothing is deleted; archive and dismiss only change status.
