# Backward-compatible re-export. The YouTube fetcher now lives in
# contentpilot.fetchers.youtube as one adapter behind the common fetcher interface
# (contentpilot.fetchers.base.RawContent); this module keeps the original import
# path working unchanged.
from contentpilot.fetchers.base import RawContent
from contentpilot.fetchers.youtube import extract_video_id, fetch_metadata, fetch_transcript

__all__ = ["RawContent", "extract_video_id", "fetch_metadata", "fetch_transcript"]
