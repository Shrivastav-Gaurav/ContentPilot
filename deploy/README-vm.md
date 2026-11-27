# ContentPilot on the VM

ContentPilot runs on `postpilot-vm` (e2-micro, Debian 13) next to PostPilot and is reached only over
Tailscale. **The VM's database is the only ContentPilot database.** The laptop runs one job, the
LinkedIn saved-posts scan (it needs your logged-in browser), which pushes URLs to the VM.

It copies PostPilot's setup: a dedicated app user owns a git clone in its home folder, systemd runs
it (`Restart=always`, `RestartSec=10`, `NoNewPrivileges`, `PrivateTmp`), updates are `git pull` +
restart, logs are in journald. Shared VM setup (the 1 GB swap file, Tailscale, unattended-upgrades,
the `postpilot` services) is PostPilot's and is not repeated or changed here; see PostPilot's README.

```
Android phone ── Share ──▶ https://postpilot-vm.tail7aeda9.ts.net/share ──┐
Tablet / laptop browser ─▶ https://postpilot-vm.tail7aeda9.ts.net/        ├ tailscale serve :443 (tailnet only)
Laptop scan (every 6 h) ─▶ https://postpilot-vm.tail7aeda9.ts.net/api/ingest ┘
     /            -> contentpilot-web    Streamlit  127.0.0.1:8502
     /api /share /manifest.json /sw.js /pwa -> contentpilot-api  uvicorn  127.0.0.1:8601
                     contentpilot-worker (triage + embedding sweep, daily rules)
     data/contentpilot.sqlite3 ── nightly 03:30 ──▶ /home/contentpilot/contentpilot-backups (7 days)

PostPilot (unchanged): postpilot + postpilot-web, http://100.78.225.111:4000
```

Both ContentPilot servers bind to 127.0.0.1. `tailscale serve` (never `funnel`) publishes them to the
tailnet only; no firewall ports are opened. Ports live in `deploy/contentpilot.env`
(`STREAMLIT_SERVER_PORT=8502`, `CP_API_PORT=8601`) and must match the serve commands below
(`tests/test_deploy_config.py` checks this).

## 1. First deploy (admin account with sudo)

```bash
sudo apt-get install -y python3-venv sqlite3
sudo useradd -m -s /bin/bash contentpilot
sudo -u contentpilot git clone https://github.com/Shrivastav-Gaurav/ContentPilot.git /home/contentpilot/ContentPilot
sudo bash /home/contentpilot/ContentPilot/deploy/install.sh --no-start   # venv, requirements-vm.txt, units
```

`requirements-vm.txt` has no chromadb, sentence-transformers or playwright. Search on the VM is stored
embeddings + SQLite FTS (`CONTENTPILOT_SEARCH=hybrid` in `deploy/contentpilot.env`).

Swap: PostPilot's setup already added a 1 GB `/swapfile`; ContentPilot adds none.

## 2. Private HTTPS with `tailscale serve`

Requires HTTPS certificates on for the tailnet (admin console → DNS → HTTPS Certificates).
PostPilot does not use `tailscale serve` (it is plain HTTP on :4000), so ContentPilot takes :443:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8502
sudo tailscale serve --bg --https=443 --set-path=/api http://127.0.0.1:8601/api
sudo tailscale serve --bg --https=443 --set-path=/share http://127.0.0.1:8601/share
sudo tailscale serve --bg --https=443 --set-path=/manifest.json http://127.0.0.1:8601/manifest.json
sudo tailscale serve --bg --https=443 --set-path=/sw.js http://127.0.0.1:8601/sw.js
sudo tailscale serve --bg --https=443 --set-path=/pwa http://127.0.0.1:8601/pwa
tailscale serve status
```

Settings persist across reboots. Remove ContentPilot's serving entirely with
`sudo tailscale serve --https=443 off` (PostPilot is unaffected either way).

## 3. Everyday commands

| What | Command (on the VM, admin account) |
| --- | --- |
| Status | `systemctl status contentpilot-web contentpilot-api contentpilot-worker` |
| Logs (follow) | `sudo journalctl -u contentpilot-web -u contentpilot-api -u contentpilot-worker -f` |
| Logs, one unit, this boot | `sudo journalctl -u contentpilot-worker -b --no-pager` |
| Restart | `sudo systemctl restart contentpilot-web contentpilot-api contentpilot-worker` |
| Update after a push | `sudo -u contentpilot git -C /home/contentpilot/ContentPilot pull && sudo bash /home/contentpilot/ContentPilot/deploy/install.sh` |
| Health | `curl -s http://127.0.0.1:8601/api/health` |
| Memory | `free -m; systemctl status contentpilot-web contentpilot-api contentpilot-worker postpilot postpilot-web \| grep -E "●\|Memory"` |
| Spend today / all time | `sudo -u contentpilot -H bash -c 'cd ~/ContentPilot && venv/bin/python -m scripts.v3_spend'` |
| Backup now | `sudo systemctl start contentpilot-backup && sudo ls -lh /home/contentpilot/contentpilot-backups` |
| Next backup | `systemctl list-timers contentpilot-backup.timer` |

Memory: each ContentPilot unit has `MemoryHigh`/`MemoryMax` (web 280/380 MB, api and worker 150/220 MB)
and `OOMScoreAdjust=500`, so under pressure the kernel throttles or kills ContentPilot, not PostPilot.

### Restore a backup (one line)

```bash
sudo systemctl stop contentpilot-web contentpilot-api contentpilot-worker && sudo -u contentpilot cp /home/contentpilot/contentpilot-backups/contentpilot-YYYYMMDD-HHMMSS.sqlite3 /home/contentpilot/ContentPilot/data/contentpilot.sqlite3 && sudo rm -f /home/contentpilot/ContentPilot/data/contentpilot.sqlite3-wal /home/contentpilot/ContentPilot/data/contentpilot.sqlite3-shm && sudo systemctl start contentpilot-web contentpilot-api contentpilot-worker
```

Pick the file with `sudo ls /home/contentpilot/contentpilot-backups`. Backups use SQLite's online
backup API (same as `sqlite3 .backup`) and are checked with `PRAGMA integrity_check`.

## 4. The API

```bash
curl -s https://postpilot-vm.tail7aeda9.ts.net/api/health                          # no token
curl -s -X POST https://postpilot-vm.tail7aeda9.ts.net/api/ingest \
     -H "X-Ingest-Token: $INGEST_TOKEN" -H "Content-Type: application/json" \
     -d '{"urls": ["https://www.linkedin.com/posts/..."], "intent": "optional"}'
```

Missing or wrong token: 401. Each URL comes back `added`, `duplicate` (already saved, not fetched
again), `blocked` (login wall) or `failed`. Up to 50 URLs per call.

## 5. Install on Android (share target)

1. Phone: Tailscale app installed, signed in to this tailnet, connected.
2. Chrome on the phone: open `https://postpilot-vm.tail7aeda9.ts.net/share`.
3. Chrome menu ⋮ → **Install app** (or **Add to Home screen** → **Install**). Open it once.
4. LinkedIn app: on a post, **Share** → **ContentPilot** (under "More" the first time). The page
   shows the link; optionally say what you want from it; tap **Add**, then **Added - open Inbox**.

If ContentPilot isn't in the Share menu yet: open the installed app once, then fully close and reopen
Chrome; Android registers share targets when the web app is installed (`chrome://webapks` lists it).

## 6. The laptop: scan and push

Laptop `.env`: `CONTENTPILOT_VM_URL=https://postpilot-vm.tail7aeda9.ts.net` and the same
`INGEST_TOKEN` as the VM.

```powershell
venv\Scripts\python -m scripts.scan_and_push --dry-run        # scan only, prints URLs
venv\Scripts\python -m scripts.scan_and_push                  # scan + push to the VM
powershell -ExecutionPolicy Bypass -File deploy\windows\register-scan-task.ps1   # at logon + every 6 h
Get-Content data\scan_and_push.log -Tail 20
```

`scan_and_push` cannot open a database (sqlite3 is disabled in that process). The laptop's old
database is renamed `data/contentpilot.sqlite3.moved-to-vm` so nothing writes to it.

## 7. One-time data move (done once, kept for reference)

Laptop: stop the local app, back up `data/contentpilot.sqlite3`, then copy the DB, `data/notes.md`,
the resume `data/*.docx` and a `.env` with only `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`,
`SYNTHESIS_MODEL`, `OPENAI_API_KEY`, `INGEST_TOKEN` to `/home/contentpilot/ContentPilot/` (owned by
`contentpilot`, `.env` mode 600). Then set the cap and start:

```bash
sudo -u contentpilot -H bash -c 'cd ~/ContentPilot && venv/bin/python -c "from contentpilot.db import get_connection; from contentpilot.copilot import spend; c=get_connection(); spend.set_daily_cap(c, 0.25); c.commit(); print(spend.get_daily_cap(c))"'
sudo systemctl start contentpilot-web contentpilot-api contentpilot-worker
```

## 8. Daily email, Sunday summary, keep-or-dismiss (Sprint 5)

Built inside the worker (no new service), from the database only: no model call, spend stays $0.
Times are Asia/Kolkata even though the VM clock is UTC: the daily email goes out from **07:30**, the
Sunday summary from **18:00 Sunday**. The worker checks every 5 minutes, so a restart or a late start
still sends that day, and `digest_log` (one row per kind and local day) makes it at most once.

Add these to `/home/contentpilot/ContentPilot/.env` (nano, mode stays 600; the same Gmail account PostPilot uses):

```
GMAIL_USER=...
GMAIL_APP_PASSWORD=...
DIGEST_TO=...
APP_BASE_URL=https://postpilot-vm.tail7aeda9.ts.net
```

If any is missing the worker logs one line per day (`digest not sent: missing ...`) and carries on.

```bash
# print today's email from a copy of the DB; sends nothing (laptop or VM)
sudo -u contentpilot -H bash -c 'cd ~/ContentPilot && venv/bin/python -m scripts.send_digest --dry-run'
# send it now (still once per day; add --again to send a second one); --kind weekly for the summary
sudo -u contentpilot -H bash -c 'cd ~/ContentPilot && venv/bin/python -m scripts.send_digest --send-now'
sudo journalctl -u contentpilot-worker -b --no-pager | grep -i digest
```

**What goes in the daily email** (a section is left out when empty; nothing at all is sent if all are empty):
due today / overdue posts (max 10, most overdue first, grouped by type); the top 3 jobs to apply to (existing
fit scores, posted in the last 60 days, not applied / deferred / not a fit; nothing is re-scored); and
"Decide". The Sunday summary has the week's numbers (added, filed/done, archived, dismissed, jobs applied and
deferred, open by type), the top 3 jobs and "Decide".

**Two ignored reminders.** Each time a post is listed in the *due* section, `post_state.reminders_sent` goes up
by one; any action you take on it sets it back to 0. At 2 it leaves the due list and shows up under "Decide" in
the email and in a **Keep or dismiss?** strip at the top of the Inbox. **Keep** resets the counter and moves the
due date 7 days on (from today if it was overdue); **Dismiss** sets it dismissed (still found by Search).
Nothing is dismissed automatically. The jobs list is not counted, so a good job can stay in the email.

`data/notes.md` lives on the VM: the Done log has a **Download notes.md** button. "Save to notes file" is
idempotent (a post already saved is not appended twice).
