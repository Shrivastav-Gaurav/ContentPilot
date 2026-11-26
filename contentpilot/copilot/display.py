"""Small formatting helpers shared by the Streamlit pages. No Streamlit, no I/O."""
import re
from datetime import datetime

from contentpilot.copilot.rules import TYPE_LABELS, due_label

_MD_SPECIAL = re.compile(r"([\\`*_\[\]#<>~|$])")

ACTION_LABELS = {
    "shortlist": "Shortlisted",
    "file_to_prep": "Filed to Prep",
    "send_postpilot": "Sent to PostPilot",
    "save_notes": "Saved to notes file",
    "read": "Marked read",
    "park": "Parked",
    "dismiss": "Dismissed",
    "archive": "Archived",
    "apply_pack_copied": "Apply pack copied",
    "job_status": "Job status set",
}

STATUS_LABELS = {"open": "Open", "done": "Done", "read": "Read", "parked": "Parked",
                 "archived": "Archived", "dismissed": "Dismissed"}

_TYPE_COLORS = {"job": "blue", "learning": "green", "post_idea": "violet", "tool": "orange", "noise": "gray"}


def md(text: str | None) -> str:
    """Escape text for st.markdown (titles contain |, *, $ and [ ] often)."""
    return _MD_SPECIAL.sub(r"\\\1", text or "")


def fmt_date(ts: int | None) -> str:
    if not ts:
        return "no date"
    d = datetime.fromtimestamp(ts)
    return f"{d:%b} {d.day}, {d.year}"


def type_badge(post_type: str | None) -> str:
    if not post_type:
        return ":gray-badge[Not sorted]"
    return f":{_TYPE_COLORS.get(post_type, 'gray')}-badge[{TYPE_LABELS.get(post_type, post_type)}]"


def due_badge(due_at: int | None, now: int) -> str:
    label = due_label(due_at, now)
    if not label:
        return ""
    color = "red" if label.startswith("overdue") else "orange" if label == "due today" else "gray"
    return f":{color}-badge[{label}]"


def status_badge(status: str | None) -> str:
    color = {"open": "blue", "archived": "gray", "dismissed": "gray"}.get(status or "", "green")
    return f":{color}-badge[{STATUS_LABELS.get(status or '', status or '?')}]"


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action.replace("_", " ").capitalize())
