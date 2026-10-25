import datetime
from urllib.parse import parse_qs, urlparse

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    CouldNotRetrieveTranscript,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
)

from contentpilot.fetchers.base import (
    FetchBlockedError,
    FetchNoTranscriptError,
    FetchUnavailableError,
    RawContent,
)

_LANGUAGE_PREFERENCE = ["en", "en-US", "en-GB"]


def matches(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    return host in ("youtube.com", "music.youtube.com", "youtu.be")


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")

    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
        if candidate:
            return candidate

    if host in ("youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            query = parse_qs(parsed.query)
            values = query.get("v")
            if values and values[0]:
                return values[0]

        for prefix in ("/shorts/", "/embed/", "/live/"):
            if parsed.path.startswith(prefix):
                candidate = parsed.path[len(prefix):].strip("/").split("/")[0]
                if candidate:
                    return candidate

    raise ValueError(f"Could not extract a YouTube video ID from: {url}")


def _canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def fetch_metadata(url: str) -> dict:
    video_id = extract_video_id(url)
    ydl_opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(_canonical_url(video_id), download=False)

    return {
        "title": info.get("title"),
        "author_name": info.get("uploader") or info.get("channel"),
        "author_channel_id": info.get("channel_id"),
        "author_url": info.get("channel_url") or info.get("uploader_url"),
        "published_at": info.get("timestamp") or _parse_upload_date(info.get("upload_date")),
        "duration_sec": info.get("duration"),
    }


def _parse_upload_date(upload_date: str | None) -> int | None:
    if not upload_date or len(upload_date) != 8:
        return None
    try:
        dt = datetime.datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def fetch(url: str) -> RawContent:
    video_id = extract_video_id(url)

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=_LANGUAGE_PREFERENCE)
    except NoTranscriptFound:
        try:
            fetched = YouTubeTranscriptApi().fetch(video_id)
        except (TranscriptsDisabled, NoTranscriptFound) as exc:
            raise FetchNoTranscriptError(f"No transcript available for video {video_id}") from exc
        except RequestBlocked as exc:
            raise FetchBlockedError(f"Blocked while fetching video {video_id}: {exc}") from exc
        except CouldNotRetrieveTranscript as exc:
            raise FetchUnavailableError(f"Video {video_id} unavailable: {exc}") from exc
    except TranscriptsDisabled as exc:
        raise FetchNoTranscriptError(f"No transcript available for video {video_id}") from exc
    except RequestBlocked as exc:
        raise FetchBlockedError(f"Blocked while fetching video {video_id}: {exc}") from exc
    except CouldNotRetrieveTranscript as exc:
        # Catches VideoUnavailable, InvalidVideoId, AgeRestricted, etc. — all terminal, never retry.
        raise FetchUnavailableError(f"Video {video_id} unavailable: {exc}") from exc

    segments = fetched.to_raw_data()
    full_text = " ".join(segment["text"].strip() for segment in segments)

    try:
        metadata = fetch_metadata(url)
    except Exception:
        metadata = {}

    return RawContent(
        source_type="youtube",
        external_id=video_id,
        canonical_url=_canonical_url(video_id),
        title=metadata.get("title"),
        author_name=metadata.get("author_name"),
        author_channel_id=metadata.get("author_channel_id"),
        author_url=metadata.get("author_url"),
        published_at=metadata.get("published_at"),
        duration_sec=metadata.get("duration_sec"),
        language=getattr(fetched, "language_code", None),
        text=full_text,
    )


# Backward-compatible alias; existing callers imported this name from contentpilot.transcript.
fetch_transcript = fetch
