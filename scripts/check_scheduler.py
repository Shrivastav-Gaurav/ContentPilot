"""Smoke test: confirm the LinkedIn saved-scan APScheduler job registers
correctly, independent of LINKEDIN_SCAN_ENABLED. Run with:
    python -m scripts.check_scheduler
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from contentpilot import config
from contentpilot.worker import trigger_saved_scan


def main() -> None:
    load_dotenv()

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

    jobs = scheduler.get_jobs()
    print(f"Registered jobs: {len(jobs)}")
    for job in jobs:
        print(f"  id={job.id!r} trigger={job.trigger} next_run={job.next_run_time}")

    assert len(jobs) == 1, f"Expected exactly 1 job, got {len(jobs)}"
    assert jobs[0].id == "linkedin_saved_scan"
    print(f"\nPASS — job registered with cron='{cron}' (UTC)")

    scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
