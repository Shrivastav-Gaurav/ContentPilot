# ContentPilot on the VM

ContentPilot runs on the Google Cloud VM (e2-micro, 1 GB RAM) next to PostPilot and is reached
only over Tailscale. **The VM's database is the only database.** The laptop runs one job: the
LinkedIn saved-posts scan (it needs your logged-in browser), which pushes URLs to the VM.

```
Android phone ── Share ──▶ https://<vm>.<tailnet>.ts.net/share ─┐
Tablet / laptop browser ─▶ https://<vm>.<tailnet>.ts.net/       ├─ tailscale serve (HTTPS, tailnet only)
Laptop scan (every 6 h) ─▶ https://<vm>.<tailnet>.ts.net/api/ingest ┘
                                   │
               ┌───────────────────┴────────────────────┐
      contentpilot-web (Streamlit 127.0.0.1:8502)   contentpilot-api (uvicorn 127.0.0.1:8601)
                       contentpilot-worker (triage/embedding sweep, daily rules)
                                   │
                    data/contentpilot.sqlite3  ──nightly──▶ ~/contentpilot-backups (7 days)
```

Nothing listens on a public interface: both servers bind to 127.0.0.1, `tailscale serve` (not
`funnel`) publishes them to your tailnet only, and no firewall ports are opened.

Placeholders below: `<vm>` = the VM's Tailscale name (`postpilot-vm`), `<tailnet>` = your tailnet
name (from `tailscale status --json | grep MagicDNSSuffix`), `~/ContentPilot` = the repo on the VM.

---

## 1. First deploy

```bash
# on the VM
sudo apt-get update && sudo apt-get install -y python3-venv git
git clone https://github.com/Shrivastav-Gaurav/ContentPilot.git ~/ContentPilot
cd ~/ContentPilot
bash deploy/install.sh --no-start        # venv + requirements-vm.txt + systemd units (not started yet)
```

`requirements-vm.txt` leaves out chromadb, sentence-transformers and playwright. Search runs on
stored embeddings + SQLite FTS (`CONTENTPILOT_SEARCH=hybrid`, set in `deploy/contentpilot.env`).

### Swap (only if `free -m` shows under 300 MB available with PostPilot running)

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf && sudo sysctl -p /etc/sysctl.d/99-swappiness.conf
```

### Move the data from the laptop (one time)

On the laptop (PowerShell, in the repo, with the local app stopped), send the files with Taildrop
(works between your own tailnet devices; no SSH needed):

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" file cp data\contentpilot.sqlite3 data\notes.md (Get-Item data\*.docx) .env.vm postpilot-vm:
```

Then on the VM, receive them into the repo and put the `.env` in place:

```bash
cd ~/ContentPilot && sudo tailscale file get --conflict=overwrite data/ && sudo chown $(id -un): data/* \
  && mv data/.env.vm .env && chmod 600 .env && ls -la data .env
```

The VM's `.env` needs: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `SYNTHESIS_MODEL`,
`OPENAI_API_KEY`, `INGEST_TOKEN` (long random, e.g. `python -c "import secrets; print(secrets.token_urlsafe(48))"`).
Then on the VM:

```bash
cd ~/ContentPilot && chmod 600 .env
venv/bin/python -c "from contentpilot.db import get_connection; from contentpilot.copilot import spend; c=get_connection(); spend.set_daily_cap(c, 0.25); c.commit(); print('cap', spend.get_daily_cap(c))"
sudo systemctl start contentpilot-web contentpilot-api contentpilot-worker
curl -s http://127.0.0.1:8601/api/health
```

## 2. Private HTTPS with `tailscale serve`

HTTPS certificates must be on for the tailnet (admin console → DNS → enable MagicDNS and HTTPS).
Check first what PostPilot already serves: `tailscale serve status`.

**A. Port 443 is free** (PostPilot not on tailscale serve, or on another port):

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8502
for p in /api /share /manifest.json /sw.js /pwa; do
  sudo tailscale serve --bg --https=443 --set-path=$p http://127.0.0.1:8601$p
done
tailscale serve status
```

ContentPilot: `https://<vm>.<tailnet>.ts.net/` (Inbox at `/Inbox`, share page at `/share`).

**B. PostPilot already owns `/` on 443:** give ContentPilot its own HTTPS port (8443), so
nothing of PostPilot changes:

```bash
sudo tailscale serve --bg --https=8443 http://127.0.0.1:8502
for p in /api /share /manifest.json /sw.js /pwa; do
  sudo tailscale serve --bg --https=8443 --set-path=$p http://127.0.0.1:8601$p
done
```

ContentPilot: `https://<vm>.<tailnet>.ts.net:8443/`. (The share target and "Open Inbox" use
relative links, so they work on either port.)

`tailscale serve --bg` settings persist across reboots. Undo ContentPilot's part only with
`sudo tailscale serve --https=<port> --set-path=<path> off` per path.

## 3. Everyday commands

| What | Command (on the VM) |
| --- | --- |
| Status | `systemctl status contentpilot-web contentpilot-api contentpilot-worker` |
| Logs (follow) | `journalctl -u contentpilot-api -u contentpilot-worker -u contentpilot-web -f` |
| Logs since boot, one unit | `journalctl -u contentpilot-worker -b --no-pager` |
| Restart | `sudo systemctl restart contentpilot-web contentpilot-api contentpilot-worker` |
| Update after `git pull` | `cd ~/ContentPilot && git pull && bash deploy/install.sh` |
| Health | `curl -s http://127.0.0.1:8601/api/health` |
| Memory per service | `systemctl status contentpilot-web contentpilot-api contentpilot-worker \| grep -E "●\|Memory"` and `free -m` |
| Spend today / all time | `cd ~/ContentPilot && venv/bin/python -m scripts.v3_spend` |
| Backup now | `sudo systemctl start contentpilot-backup && ls -lh ~/contentpilot-backups` |
| Next backup | `systemctl list-timers contentpilot-backup.timer` |

Memory: each unit has `MemoryHigh`/`MemoryMax` (web 280/380 MB, api and worker 150/220 MB) and
`OOMScoreAdjust=500`, so under pressure the kernel throttles or kills ContentPilot, never PostPilot.

### Restore a backup (one line)

```bash
sudo systemctl stop contentpilot-web contentpilot-api contentpilot-worker && cp ~/contentpilot-backups/contentpilot-YYYYMMDD-HHMMSS.sqlite3 ~/ContentPilot/data/contentpilot.sqlite3 && rm -f ~/ContentPilot/data/contentpilot.sqlite3-wal ~/ContentPilot/data/contentpilot.sqlite3-shm && sudo systemctl start contentpilot-web contentpilot-api contentpilot-worker
```

(`ls ~/contentpilot-backups` to pick the file. Backups are made with SQLite's online backup API,
the same as `sqlite3 .backup`, and checked with `PRAGMA integrity_check`.)

## 4. The API

```bash
# health (no token)
curl -s https://<vm>.<tailnet>.ts.net/api/health
# add posts (token required; wrong or missing token -> 401)
curl -s -X POST https://<vm>.<tailnet>.ts.net/api/ingest -H "X-Ingest-Token: $INGEST_TOKEN" \
     -H "Content-Type: application/json" -d '{"urls": ["https://www.linkedin.com/posts/..."], "intent": "optional"}'
```

Each URL comes back `added`, `duplicate` (already saved; nothing fetched), `blocked` (login wall)
or `failed`. Up to 50 URLs per call; new posts are triaged together and embedded.

## 5. Install the app on Android (share target)

1. Phone: install Tailscale, sign in to the same tailnet, keep it connected.
2. Chrome on the phone: open `https://<vm>.<tailnet>.ts.net/share` (add `:8443` for option B).
3. Chrome menu ⋮ → **Install app** (or **Add to Home screen** → Install). Open it once.
4. In LinkedIn: on a post tap **Share → … (more) → ContentPilot**. The page shows the link;
   optionally type what you want from it, tap **Add**, then **Added - open Inbox**.

If ContentPilot is missing from the Share menu: open the installed app once, then reboot Chrome
(Android refreshes share targets lazily). `chrome://webapks` lists installed web apps.

## 6. The laptop: scan and push

The laptop keeps only the LinkedIn scan. In the laptop `.env`:
`CONTENTPILOT_VM_URL=https://<vm>.<tailnet>.ts.net` (plus `:8443` for option B) and the same
`INGEST_TOKEN` as the VM.

```powershell
venv\Scripts\python -m scripts.scan_and_push --dry-run     # scan only, prints URLs
venv\Scripts\python -m scripts.scan_and_push               # scan + push to the VM
powershell -ExecutionPolicy Bypass -File deploy\windows\register-scan-task.ps1   # at logon + every 6 h
Get-Content data\scan_and_push.log -Tail 20
```

`scan_and_push` cannot open a database (sqlite3 is disabled in that process). After the move the
laptop's database is renamed to `data/contentpilot.sqlite3.moved-to-vm`, so nothing writes to it.
