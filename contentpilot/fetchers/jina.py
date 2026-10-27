import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests

from contentpilot import config
from contentpilot.fetchers.base import FetchBlockedError, FetchUnavailableError, RawContent

_MIN_TEXT_LENGTH = 120

# LinkedIn shows generic "Join now"/"Sign in" nav chrome on EVERY public post
# page, authenticated or not — so a phrase like "join linkedin" appearing
# anywhere is not evidence of a wall; real public posts with full content
# routinely contain it (confirmed against live saved-post fetches). Only
# phrases that specifically assert access is being denied (not just offered)
# are safe markers regardless of page length.
_LOGIN_WALL_MARKERS = [
    "authwall",
    "you must log in",
    "you must sign in",
    "please log in to continue",
    "please sign in to continue",
]

# Weaker phrasing that often appears in nav chrome too, but genuinely
# indicates a wall when the page is otherwise short — i.e. there's no real
# content around it, just the wall itself.
_WEAK_LOGIN_WALL_MARKERS = [
    "sign in to linkedin",
    "join now to see",
    "log in or sign up",
]
_WEAK_MARKER_MAX_LENGTH = 2000

_NOT_FOUND_MARKERS = [
    "page not found",
    "we can't find the page",
    "we can’t find the page",
    "the page you're looking for may have been moved",
]

_TRACKING_PARAM_PREFIXES = ("utm_", "trk", "trackingid", "refid", "ref_src")


def matches(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return "linkedin.com" in host or True  # fallback adapter for any non-YouTube URL


def is_linkedin(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return "linkedin.com" in host


def canonicalize(url: str) -> str:
    parsed = urlparse(url)
    kept_query = [
        (k, v) for k, v in parse_qsl(parsed.query)
        if not any(k.lower().startswith(prefix) for prefix in _TRACKING_PARAM_PREFIXES)
    ]
    return urlunparse(parsed._replace(query=urlencode(kept_query), fragment=""))


def _looks_like_login_wall(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in _LOGIN_WALL_MARKERS):
        return True
    if len(text) <= _WEAK_MARKER_MAX_LENGTH:
        return any(marker in lowered for marker in _WEAK_LOGIN_WALL_MARKERS)
    return False


def _looks_like_not_found(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def _extract_title(text: str) -> str | None:
    match = re.search(r"^Title:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


# LinkedIn's guest-view markdown puts the author name as its own link right
# after the profile-image link, tagged with "feed-actor-name" in the trk
# param — this is the one part of the generic nav chrome that's actually
# per-post data, not boilerplate. Falls back to None (not every source is
# LinkedIn, and malformed/unusual pages shouldn't raise).
_AUTHOR_PATTERN = re.compile(
    r"\[([^\]\[]{2,100})\]\([^)]*feed-actor-name[^)]*\)"
)


def _extract_author(text: str) -> str | None:
    match = _AUTHOR_PATTERN.search(text)
    if not match:
        return None
    name = match.group(1).strip()
    return name or None


def _extract_author_url(text: str) -> str | None:
    match = _AUTHOR_PATTERN.search(text)
    if not match:
        return None
    url_match = re.search(r"\(([^)]*feed-actor-name[^)]*)\)", match.group(0))
    if not url_match:
        return None
    return url_match.group(1).split("?")[0]


def fetch(url: str) -> RawContent:
    canonical = canonicalize(url)
    headers = {}
    api_key = config.jina_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.get(f"{config.JINA_READER_URL}{canonical}", headers=headers, timeout=60)
    response.raise_for_status()
    text = response.text.strip()

    if _looks_like_not_found(text):
        raise FetchUnavailableError(f"Page not found for {canonical}")

    if _looks_like_login_wall(text):
        raise FetchBlockedError(f"Login wall detected for {canonical}")

    if len(text) < _MIN_TEXT_LENGTH:
        raise FetchBlockedError(
            f"Fetched content too short ({len(text)} chars) for {canonical} — likely blocked or empty."
        )

    source_type = "linkedin" if is_linkedin(canonical) else "article"

    return RawContent(
        source_type=source_type,
        external_id=canonical,
        canonical_url=canonical,
        title=_extract_title(text),
        author_name=_extract_author(text),
        author_channel_id=None,
        author_url=_extract_author_url(text),
        published_at=None,
        duration_sec=None,
        language=None,
        text=text,
    )
