"""Scrapes post URLs from LinkedIn's "My items > Saved posts" page.

Returns URLs only — extraction is handled by the existing Jina reader +
extraction pipeline (contentpilot.fetchers.jina / contentpilot.extraction),
the same as any other ingested URL.
"""
import time

from playwright.sync_api import Page, sync_playwright

from contentpilot import config
from contentpilot.scrapers.linkedin_auth import ensure_valid_session

# LinkedIn's saved-posts list has no reliable per-item date exposed in a form
# Playwright can cleanly associate with a specific post link (the relative-time
# text like "3d •" lives in React-rendered card markup with no stable
# link-to-timestamp relationship we could pair reliably — tested directly
# against the live DOM and couldn't get a trustworthy pairing). So time_window
# is enforced as a hard cap on how many distinct posts we collect, newest
# first (LinkedIn's saved list is already newest-saved-first), rather than a
# true date cutoff. This is an approximation, not an exact "posts saved in the
# last N days" filter — it assumes a roughly steady saving cadence.
_WINDOW_URL_CAP = {"15d": 50, "30d": 100, "all": None}

_SCROLL_PAUSE_SEC = 1.5
_MAX_SCROLL_ATTEMPTS_NO_GROWTH = 3


def _parse_window(window: str) -> int | None:
    if window not in _WINDOW_URL_CAP:
        raise ValueError(f"Invalid time_window: {window!r}. Expected one of {list(_WINDOW_URL_CAP)}.")
    return _WINDOW_URL_CAP[window]


def _extract_post_urls(page: Page) -> list[str]:
    hrefs = page.eval_on_selector_all(
        'a[href*="/posts/"], a[href*="/feed/update/"]',
        "elements => elements.map(e => e.href)",
    )
    seen = set()
    urls = []
    for href in hrefs:
        clean = href.split("?")[0]
        if clean not in seen:
            seen.add(clean)
            urls.append(clean)
    return urls


def scrape_saved_post_urls(time_window: str | None = None, headless: bool = False) -> list[str]:
    """Scrolls LinkedIn's saved-posts page and returns distinct post URLs,
    newest-saved first, capped per time_window (see _WINDOW_URL_CAP above
    for why this is a count cap rather than a true date cutoff).

    time_window: "15d" | "30d" | "all".
    """
    window = time_window or config.linkedin_scan_window()
    url_cap = _parse_window(window)

    with sync_playwright() as p:
        browser, context, page = ensure_valid_session(p, headless=headless)
        try:
            page.goto(config.LINKEDIN_SAVED_POSTS_URL, wait_until="domcontentloaded")
            time.sleep(2)

            no_growth_count = 0
            previous_count = 0

            while no_growth_count < _MAX_SCROLL_ATTEMPTS_NO_GROWTH:
                urls = _extract_post_urls(page)
                if url_cap is not None and len(urls) >= url_cap:
                    break
                if len(urls) <= previous_count:
                    no_growth_count += 1
                else:
                    no_growth_count = 0
                previous_count = len(urls)

                page.mouse.wheel(0, 3000)
                time.sleep(_SCROLL_PAUSE_SEC)

            urls = _extract_post_urls(page)
            return urls[:url_cap] if url_cap is not None else urls
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    urls = scrape_saved_post_urls()
    print(f"[test:saved_posts] found {len(urls)} saved post URLs")
    for u in urls[:10]:
        print(" ", u)
