"""Laptop job: scan LinkedIn saved posts (your logged-in browser) and push the URLs to the VM.

    python -m scripts.scan_and_push              # scan, then POST to $CONTENTPILOT_VM_URL/api/ingest
    python -m scripts.scan_and_push --dry-run    # scan and list the URLs; send nothing
    python -m scripts.scan_and_push --window 15d

Needs in .env: CONTENTPILOT_VM_URL (e.g. https://postpilot-vm.<tailnet>.ts.net) and INGEST_TOKEN.
Never touches a local database: sqlite3.connect is disabled for this process. The VM dedupes,
fetches, triages and embeds. Log: data/scan_and_push.log (Task Scheduler runs this every 6 hours).
"""
import argparse
import logging
import os
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv

from contentpilot import config

BATCH = 10
TIMEOUT_SEC = 600
LOG_PATH = os.path.join(config.DATA_DIR, "scan_and_push.log")
log = logging.getLogger("scan_and_push")


class LocalDatabaseRefused(RuntimeError):
    pass


def _no_local_db(*args, **kwargs):
    raise LocalDatabaseRefused("scan_and_push must not open a local database (the VM is the only database)")


def _setup_logging() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stdout is not None:  # pythonw has no console
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        log.addHandler(console)
    log.setLevel(logging.INFO)


def push(urls: list[str], vm_url: str, token: str, post=requests.post) -> dict:
    """POST URLs to the VM in batches. Returns summed counts per status."""
    totals: dict[str, int] = {}
    endpoint = vm_url.rstrip("/") + "/api/ingest"
    for i in range(0, len(urls), BATCH):
        batch = urls[i:i + BATCH]
        resp = post(endpoint, json={"urls": batch}, headers={"X-Ingest-Token": token}, timeout=TIMEOUT_SEC)
        if resp.status_code == 401:
            raise RuntimeError("the VM rejected INGEST_TOKEN (401); copy the VM's token into the laptop .env")
        resp.raise_for_status()
        body = resp.json()
        for status, n in body.get("counts", {}).items():
            totals[status] = totals.get(status, 0) + n
        for r in body.get("results", []):
            if r["status"] in ("added", "blocked", "failed"):
                log.info("  %-9s %s %s", r["status"], r["url"], r.get("title") or r.get("detail") or "")
    return totals


def main(argv: list[str] | None = None, scrape=None, post=requests.post) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", default=None, help="15d | 30d | all (default LINKEDIN_SCAN_WINDOW, 30d)")
    parser.add_argument("--dry-run", action="store_true", help="scan only; send nothing")
    parser.add_argument("--headless", action="store_true", help="hide the browser (LinkedIn may block it)")
    args = parser.parse_args(argv)

    load_dotenv()
    sqlite3.connect = _no_local_db  # this job never writes locally
    _setup_logging()
    vm_url = os.environ.get("CONTENTPILOT_VM_URL", "").strip()
    token = config.ingest_token()
    if not args.dry_run and (not vm_url or not token):
        log.error("CONTENTPILOT_VM_URL and INGEST_TOKEN must be set in .env")
        return 2

    started = time.time()
    try:
        if scrape is None:
            from contentpilot.scrapers.saved_posts import scrape_saved_post_urls as scrape
        urls = scrape(time_window=args.window, headless=args.headless)
    except Exception as exc:
        log.error("scan failed: %s: %s", type(exc).__name__, exc)
        return 1
    log.info("scan found %d saved post URL(s) in %.0fs", len(urls), time.time() - started)
    if args.dry_run:
        for u in urls:
            log.info("  %s", u)
        return 0
    if not urls:
        return 0
    try:
        totals = push(urls, vm_url, token, post=post)
    except Exception as exc:
        log.error("push to %s failed: %s: %s", vm_url, type(exc).__name__, exc)
        return 1
    log.info("pushed to VM: %s", ", ".join(f"{k} {v}" for k, v in sorted(totals.items())) or "nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
