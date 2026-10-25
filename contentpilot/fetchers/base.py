from dataclasses import dataclass


class FetchBlockedError(ValueError):
    """Content exists but could not be fetched cleanly (login wall, empty output, etc.)."""


class FetchUnavailableError(ValueError):
    """Content does not exist or will never be fetchable (deleted, invalid ID)."""


class FetchNoTranscriptError(FetchUnavailableError):
    """The video exists but has no transcript available — never retry."""


@dataclass
class RawContent:
    source_type: str
    external_id: str
    canonical_url: str
    title: str | None
    author_name: str | None
    author_channel_id: str | None
    author_url: str | None
    published_at: int | None
    duration_sec: int | None
    language: str | None
    text: str
    fetch_status: str = "ok"
