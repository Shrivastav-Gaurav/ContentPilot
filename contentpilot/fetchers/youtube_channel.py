from urllib.parse import urlparse

import yt_dlp


def is_channel_url(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    if host not in ("youtube.com", "music.youtube.com"):
        return False
    path = urlparse(url).path
    return path.startswith("/@") or path.startswith("/channel/") or path.startswith("/playlist") or path.startswith("/c/")


def _videos_tab_url(channel_url: str) -> str:
    """@handle and /channel/ID URLs resolve to a tabs page (Videos/Live/Shorts);
    target the Videos tab explicitly so entries are actual videos, not nested playlists."""
    path = urlparse(channel_url).path.rstrip("/")
    if path.startswith("/@") or path.startswith("/channel/") or path.startswith("/c/"):
        if not path.endswith("/videos"):
            return channel_url.rstrip("/") + "/videos"
    return channel_url


def _flat_extract_entries(url: str, max_videos: int) -> list[dict]:
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": max_videos,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info.get("entries") or []


def list_video_urls(channel_url: str, max_videos: int = 25) -> list[str]:
    """Flat-extract video IDs from a channel/playlist URL, newest first, no downloads."""
    target_url = _videos_tab_url(channel_url)
    entries = _flat_extract_entries(target_url, max_videos)

    video_ids = []
    for entry in entries:
        if entry is None or entry.get("_type") == "playlist":
            continue
        video_id = entry.get("id")
        if video_id:
            video_ids.append(video_id)
        if len(video_ids) >= max_videos:
            break

    return [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids]


def get_channel_id(channel_url: str) -> str | None:
    ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True, "playlistend": 1}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
    return info.get("channel_id") or info.get("id")
