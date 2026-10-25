from contentpilot.fetchers import jina, youtube
from contentpilot.fetchers.base import RawContent


def fetch(url: str) -> RawContent:
    if youtube.matches(url):
        return youtube.fetch(url)
    return jina.fetch(url)
