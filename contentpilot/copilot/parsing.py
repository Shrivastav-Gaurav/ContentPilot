"""Facts we can read from fetched text without an LLM call.

Jina Reader output starts with a small header block:
    Title: ...
    URL Source: ...
    Published Time: 2026-10-20T06:38:37.935Z
    Markdown Content:
    ## Flowstate's Post
"""
import re
from datetime import datetime, timezone

_PUBLISHED_RE = re.compile(r"^Published Time:\s*(\d{4}-\d{2}-\d{2}(?:T[0-9:.]+)?Z?)", re.MULTILINE)
_TITLE_RE = re.compile(r"^Title:\s*(.+)$", re.MULTILINE)
# "## Flowstate's Post" / "## Flowstate’s Post"
_POST_HEADER_RE = re.compile(r"^##\s+(.+?)[’']s Post\s*$", re.MULTILINE)
# "Evaluating ... | Stan Fedotov, PhD posted on the topic | LinkedIn"
_TITLE_AUTHOR_RE = re.compile(r"\|\s*([^|]+?)\s+posted on the topic\s*\|")
_HEADER_SCAN_CHARS = 4000


def parse_published_at(text: str | None) -> int | None:
    """Unix seconds from the 'Published Time:' header line, or None."""
    if not text:
        return None
    match = _PUBLISHED_RE.search(text[:_HEADER_SCAN_CHARS])
    if not match:
        return None
    raw = match.group(1).rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def parse_title(text: str | None) -> str | None:
    if not text:
        return None
    match = _TITLE_RE.search(text[:_HEADER_SCAN_CHARS])
    return match.group(1).strip() if match else None


def parse_publisher(text: str | None) -> str | None:
    """Publisher name from the LinkedIn post header or the page title."""
    if not text:
        return None
    head = text[:_HEADER_SCAN_CHARS]
    match = _POST_HEADER_RE.search(head)
    if match:
        return match.group(1).strip()
    title = parse_title(head)
    if title:
        match = _TITLE_AUTHOR_RE.search(title)
        if match:
            return match.group(1).strip()
    return None


def body_text(text: str | None) -> str:
    """Post body without Jina's header block."""
    if not text:
        return ""
    marker = "Markdown Content:"
    idx = text.find(marker)
    return text[idx + len(marker):].strip() if idx != -1 else text.strip()


# --- titles for display (never written back to sources.title) -----------------

_LINKEDIN_SUFFIX_RE = re.compile(r"\s*\|\s*LinkedIn\s*$", re.IGNORECASE)
_POSTED_SUFFIX_RE = re.compile(r"\s*\|\s*[^|]+?\s+posted on the topic\s*$", re.IGNORECASE)
_COMMENTS_SUFFIX_RE = re.compile(r"\s*\|\s*[\d,.]+[kK]?\s+comments?\s*$", re.IGNORECASE)
_LAST_SEGMENT_RE = re.compile(r"\s*\|\s*([^|]+)$")
_POST_N_RE = re.compile(r"^POST\s+\d+:?$", re.IGNORECASE)
_MIN_TITLE_CHARS = 15


def clean_title(title: str | None, publisher: str | None = None) -> str:
    """Strip LinkedIn page-title suffixes: '| <name> | 12 comments', '| LinkedIn',
    '| <name> posted on the topic', and a trailing '| <publisher>'."""
    t = (title or "").strip()
    pub = (publisher or "").strip().lower()
    while True:
        before = t
        t = _LINKEDIN_SUFFIX_RE.sub("", t)
        t = _POSTED_SUFFIX_RE.sub("", t)
        if _COMMENTS_SUFFIX_RE.search(t):
            t = _COMMENTS_SUFFIX_RE.sub("", t)
            t = _LAST_SEGMENT_RE.sub("", t)  # the "| <name>" that precedes the count
        if pub and (m := _LAST_SEGMENT_RE.search(t)):
            seg = m.group(1).strip().lower()
            if seg and (seg.startswith(pub) or pub.startswith(seg)):
                t = t[:m.start()]
        t = t.strip()
        if t == before:
            return t


def display_title(title: str | None, summary: str | None, publisher: str | None = None) -> str:
    """Title to show on screen. Falls back to the triage summary when the cleaned
    title is too short or is a placeholder like 'POST 11:'."""
    t = clean_title(title, publisher)
    if len(t) < _MIN_TITLE_CHARS or _POST_N_RE.match(t):
        if summary and summary.strip():
            return summary.strip()
    return t or "Untitled"
