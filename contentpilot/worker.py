"""Standalone ingest worker. Run with: python -m contentpilot.worker

Streamlit never runs ingestion inline; it only enqueues jobs (storage.create_ingest_job)
and polls status. This process is the only writer to Chroma.
"""
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

from contentpilot import config
from contentpilot.db import get_connection
from contentpilot.extraction import ExtractionValidationError, extract_tpm_lens
from contentpilot.fetchers.base import FetchBlockedError, FetchNoTranscriptError, FetchUnavailableError
from contentpilot.fetchers.router import fetch as fetch_content
from contentpilot.prompts import tpm_lens
from contentpilot.storage import create_ingest_job, mark_source_saved, save_card, save_source

LENS = "tpm"


def _reset_stuck_items() -> None:
    """Crash recovery: items left 'running' from a prior process get re-queued."""
    conn = get_connection()
    try:
        conn.execute("UPDATE ingest_job_items SET status = 'queued' WHERE status = 'running'")
        conn.execute(
            "UPDATE ingest_jobs SET status = 'queued' WHERE status = 'running'"
        )
        conn.commit()
    finally:
        conn.close()


def _next_queued_job() -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ingest_jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _mark_job_running(job_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE ingest_jobs SET status = 'running', started_at = ? WHERE id = ?",
            (int(time.time()), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _job_status(job_id: int) -> str:
    conn = get_connection()
    try:
        row = conn.execute("SELECT status FROM ingest_jobs WHERE id = ?", (job_id,)).fetchone()
        return row["status"] if row else "unknown"
    finally:
        conn.close()


def _job_cost_so_far(job_id: int) -> float:
    conn = get_connection()
    try:
        row = conn.execute("SELECT cost_usd FROM ingest_jobs WHERE id = ?", (job_id,)).fetchone()
        return row["cost_usd"] or 0.0 if row else 0.0
    finally:
        conn.close()


def _finalize_job(job_id: int, status: str, last_error: str | None = None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE ingest_jobs SET status = ?, last_error = ?, finished_at = ? WHERE id = ?",
            (status, last_error, int(time.time()), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_item(item_id: int, **fields) -> None:
    conn = get_connection()
    try:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [int(time.time()), item_id]
        conn.execute(
            f"UPDATE ingest_job_items SET {set_clause}, updated_at = ? WHERE id = ?", values
        )
        conn.commit()
    finally:
        conn.close()


def _increment_job_counters(job_id: int, done: int = 0, failed: int = 0, cost: float = 0.0) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE ingest_jobs
            SET done_items = done_items + ?, failed_items = failed_items + ?, cost_usd = cost_usd + ?
            WHERE id = ?
            """,
            (done, failed, cost, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _already_has_card(canonical_url: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT cards.id FROM cards
            JOIN sources ON cards.source_id = sources.id
            WHERE sources.canonical_url = ? AND cards.lens = ? AND cards.prompt_version = ?
            """,
            (canonical_url, LENS, tpm_lens.VERSION),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _process_item(job_id: int, item: dict, cost_cap_usd: float, job_type: str) -> None:
    item_id = item["id"]
    url = item["source_url"]

    if _job_status(job_id) in ("cancelled",):
        _mark_item(item_id, status="queued")
        return

    if _job_cost_so_far(job_id) >= cost_cap_usd:
        _mark_item(item_id, status="queued")
        return

    _mark_item(item_id, status="running", attempts=item["attempts"] + 1)

    max_attempts = config.WORKER_MAX_ATTEMPTS
    attempt = item["attempts"] + 1

    try:
        raw = fetch_content(url)

        if _already_has_card(raw.canonical_url):
            source_id = save_source(raw)
            if job_type == "saved_scan":
                mark_source_saved(source_id)
            _mark_item(item_id, status="skipped_duplicate")
            return

        source_id = save_source(raw)
        if job_type == "saved_scan":
            mark_source_saved(source_id)
        result = extract_tpm_lens(raw.text)
        card_id = save_card(source_id, LENS, result)

        cost = result.cost_usd or 0.0
        _mark_item(item_id, status="done", card_id=card_id, error=None)
        _increment_job_counters(job_id, done=1, cost=cost)

    except FetchNoTranscriptError as exc:
        _mark_item(item_id, status="no_transcript", error=str(exc))
        _increment_job_counters(job_id, failed=1)
    except FetchUnavailableError as exc:
        _mark_item(item_id, status="unavailable", error=str(exc))
        _increment_job_counters(job_id, failed=1)
    except FetchBlockedError as exc:
        _mark_item(item_id, status="blocked", error=str(exc))
        _increment_job_counters(job_id, failed=1)
    except ValueError as exc:
        # malformed URL or similar — terminal, never retry
        _mark_item(item_id, status="unavailable", error=str(exc))
        _increment_job_counters(job_id, failed=1)
    except ExtractionValidationError as exc:
        _log_failed_extraction(url, exc)
        _retry_or_fail(job_id, item_id, attempt, max_attempts, str(exc))
    except Exception as exc:
        _retry_or_fail(job_id, item_id, attempt, max_attempts, str(exc))


def _log_failed_extraction(url: str, exc: ExtractionValidationError) -> None:
    if not getattr(exc, "cost_usd", 0):
        return
    from contentpilot.copilot.spend import log_cost
    conn = get_connection()
    try:
        log_cost(conn, "extraction", f"failed:{url}@{time.time_ns()}", exc.model, exc.cost_usd,
                 {"url": url, "failed": True}, version=tpm_lens.VERSION)
        conn.commit()
    finally:
        conn.close()


def _retry_or_fail(job_id: int, item_id: int, attempt: int, max_attempts: int, error: str) -> None:
    if attempt >= max_attempts:
        _mark_item(item_id, status="failed", error=error)
        _increment_job_counters(job_id, failed=1)
        return

    backoff = min(30, (2 ** attempt)) + random.uniform(0, 1)
    time.sleep(backoff)
    _mark_item(item_id, status="queued", error=error)


def _next_queued_items(job_id: int, limit: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM ingest_job_items WHERE job_id = ? AND status = 'queued' "
            "ORDER BY id LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _run_job(job: dict) -> None:
    job_id = job["id"]
    job_type = job["job_type"]
    cost_cap_usd = job["cost_cap_usd"] or config.DEFAULT_COST_CAP_USD
    _mark_job_running(job_id)

    while True:
        if _job_status(job_id) == "cancelled":
            break

        if _job_cost_so_far(job_id) >= cost_cap_usd:
            _finalize_job(job_id, "failed", f"Cost cap of ${cost_cap_usd:.2f} reached.")
            return

        batch = _next_queued_items(job_id, config.WORKER_CONCURRENCY)
        if not batch:
            break

        with ThreadPoolExecutor(max_workers=config.WORKER_CONCURRENCY) as executor:
            futures = [
                executor.submit(_process_item, job_id, item, cost_cap_usd, job_type)
                for item in batch
            ]
            for future in as_completed(futures):
                future.result()

    final_status = _job_status(job_id)
    if final_status == "cancelled":
        _finalize_job(job_id, "cancelled")
    elif _job_cost_so_far(job_id) >= cost_cap_usd:
        _finalize_job(job_id, "failed", f"Cost cap of ${cost_cap_usd:.2f} reached.")
    else:
        _finalize_job(job_id, "done")

    if job_type == "saved_scan":
        _triage_new_sources()


def _embed_new_posts(conn) -> None:
    """Keep meaning-based search current once it has been set up (backfill run, key present)."""
    from contentpilot.copilot import embeddings
    try:
        if embeddings.enabled() and embeddings.has_embeddings(conn):
            stats = embeddings.embed_pending(conn)
            conn.commit()
            print(f"[worker] embedded {stats['embedded']} new post(s)/note(s), ${stats['cost_usd']:.5f}")
    except Exception as exc:  # search falls back to keywords; never take the worker down
        conn.rollback()
        print(f"[worker] embedding after saved scan failed: {type(exc).__name__}")


def _triage_new_sources(llm=None) -> dict | None:
    """After a saved scan: fill facts and triage every post that has no state yet.

    Runs even if the job hit its cap or was cancelled, so whatever was saved
    reaches the Inbox. Cached posts cost nothing; the daily triage cap applies.
    """
    from contentpilot.copilot import service, triage

    conn = get_connection()
    try:
        service.backfill_facts(conn)  # only fills what is missing
        stats = service.run_triage(conn, llm=llm or triage.openrouter_llm)
        conn.commit()
        _embed_new_posts(conn)
        print(f"[worker] triage after saved scan: {stats}")
        return stats
    except Exception as exc:  # never take the worker down over triage
        conn.rollback()
        print(f"[worker] triage after saved scan failed: {exc}")
        return None
    finally:
        conn.close()


def trigger_saved_scan(time_window: str | None = None) -> int:
    """Scrapes LinkedIn saved posts and enqueues them as a saved_scan ingest job.

    Runs the (headed) Playwright scrape synchronously before returning — the
    caller (scheduler tick or a Streamlit button) blocks for the scrape, then
    the worker's normal job loop handles extraction/storage for the URLs.
    """
    from contentpilot.scrapers.saved_posts import scrape_saved_post_urls

    window = time_window or config.linkedin_scan_window()
    urls = scrape_saved_post_urls(time_window=window)
    job_id = create_ingest_job("saved_scan", "linkedin-saved-posts", urls, cost_cap_usd=config.DEFAULT_COST_CAP_USD)
    print(f"[worker] saved_scan job {job_id} enqueued with {len(urls)} URL(s)")
    return job_id


def _start_scheduler():
    if not config.linkedin_scan_enabled():
        return None

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone="UTC")
    cron = config.linkedin_scan_cron()
    minute, hour, day, month, day_of_week = cron.split()
    scheduler.add_job(
        trigger_saved_scan,
        CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week),
        id="linkedin_saved_scan",
        replace_existing=True,
    )
    scheduler.start()
    print(f"[worker] LinkedIn saved-scan scheduler armed: cron='{cron}' (UTC)")
    return scheduler


def run_forever() -> None:
    load_dotenv()
    _reset_stuck_items()
    scheduler = _start_scheduler()
    print("ContentPilot worker started. Polling for jobs...")
    try:
        while True:
            job = _next_queued_job()
            if job is None:
                time.sleep(config.WORKER_POLL_INTERVAL_SEC)
                continue
            print(f"Processing job {job['id']} ({job['job_type']}: {job['target']})")
            _run_job(job)
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    run_forever()
