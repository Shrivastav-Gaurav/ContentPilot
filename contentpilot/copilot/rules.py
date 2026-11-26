"""Due dates and automatic status rules. Pure functions, no I/O, no LLM."""
from dataclasses import dataclass

DAY = 86_400

POST_TYPES = ("job", "learning", "post_idea", "tool", "noise")
STATUSES = ("open", "done", "read", "parked", "archived", "dismissed")
JOB_USER_STATUSES = ("not_started", "in_progress", "applied", "deferred", "not_a_fit")

# Defaults; each can be overridden in the settings table as due.<key> (days).
DEFAULT_RULES = {
    "job_after_save": 3,
    "job_rank_max_age": 60,
    "job_archive_age": 90,
    "learning_after_save": 7,
    "post_idea_after_save": 14,
    "tool_resurface": 30,
    "deferred_default": 7,
}


@dataclass(frozen=True)
class Decision:
    status: str
    due_at: int | None
    reason: str


def initial_decision(
    post_type: str,
    saved_at: int,
    published_at: int | None,
    now: int,
    rules: dict | None = None,
) -> Decision:
    """Status and due date for a freshly triaged post."""
    r = {**DEFAULT_RULES, **(rules or {})}

    if post_type == "noise":
        return Decision("dismissed", None, "promotional or off-topic")

    if post_type == "job":
        if published_at and now - published_at > r["job_archive_age"] * DAY:
            return Decision("archived", None, f"job post older than {r['job_archive_age']} days")
        due = saved_at + r["job_after_save"] * DAY
        if published_at:
            due = min(due, published_at + r["job_rank_max_age"] * DAY)
        return Decision("open", max(due, now), "job")

    # A backlog post is never born overdue: its due date starts no earlier than now.
    if post_type == "learning":
        return Decision("open", max(saved_at + r["learning_after_save"] * DAY, now), "learning")
    if post_type == "post_idea":
        return Decision("open", max(saved_at + r["post_idea_after_save"] * DAY, now), "post idea")
    if post_type == "tool":
        return Decision("open", max(saved_at + r["learning_after_save"] * DAY, now), "tool")

    raise ValueError(f"unknown post_type {post_type!r}")


def reopen_due(post_type: str | None, now: int, rules: dict | None = None) -> int | None:
    """Fresh due date when a post is moved back to the Inbox."""
    r = {**DEFAULT_RULES, **(rules or {})}
    days = {
        "job": r["job_after_save"],
        "learning": r["learning_after_save"],
        "post_idea": r["post_idea_after_save"],
        "tool": r["learning_after_save"],
    }.get(post_type or "", r["learning_after_save"])
    return now + days * DAY


def should_auto_archive(post_type: str | None, status: str, published_at: int | None, now: int,
                        rules: dict | None = None) -> bool:
    """Open job posts cross the archive age while waiting."""
    r = {**DEFAULT_RULES, **(rules or {})}
    return (
        post_type == "job"
        and status == "open"
        and published_at is not None
        and now - published_at > r["job_archive_age"] * DAY
    )


def job_rankable(published_at: int | None, now: int, rules: dict | None = None) -> bool:
    """Only job posts no older than job_rank_max_age days get a fit score."""
    r = {**DEFAULT_RULES, **(rules or {})}
    return published_at is None or now - published_at <= r["job_rank_max_age"] * DAY
