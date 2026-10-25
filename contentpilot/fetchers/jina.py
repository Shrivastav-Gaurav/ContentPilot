import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests

from contentpilot import config
from contentpilot.fetchers.base import FetchBlockedError, FetchUnavailableError, RawContent

_MIN_TEXT_LENGTH = 120

_LOGIN_WALL_MARKERS = [
    "sign in to linkedin",
    "join linkedin",
    "join now to see",
    "please log in",
    "log in or sign up",
    "you must log in",
    "authwall",
]

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
    return any(marker in lowered for marker in _LOGIN_WALL_MARKERS)


def _looks_like_not_found(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def _extract_title(text: str) -> str | None:
    match = re.search(r"^Title:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


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
        author_name=None,
        author_channel_id=None,
        author_url=None,
        published_at=None,
        duration_sec=None,
        language=None,
        text=text,
    )
