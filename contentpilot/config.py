import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "contentpilot.sqlite3")
CHROMA_PATH = os.path.join(DATA_DIR, "chroma")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_MODEL_ENV = "OPENROUTER_MODEL"
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-3-haiku"

JINA_READER_URL = "https://r.jina.ai/"
JINA_API_KEY_ENV = "JINA_API_KEY"

DEFAULT_COST_CAP_USD = 1.00
DEFAULT_MAX_VIDEOS = 25
WORKER_CONCURRENCY = 3
WORKER_MAX_ATTEMPTS = 3
WORKER_POLL_INTERVAL_SEC = 2

LINKEDIN_SESSION_PATH = os.path.join(DATA_DIR, "linkedin-session.json")
LINKEDIN_SAVED_POSTS_URL = "https://www.linkedin.com/my-items/saved-posts/"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"

DEFAULT_LINKEDIN_SCAN_WINDOW = "30d"
DEFAULT_LINKEDIN_SCAN_ENABLED = False
DEFAULT_LINKEDIN_SCAN_CRON = "30 17 * * 0"  # Sunday 23:00 IST = 17:30 UTC

# Synthesis quality matters more than cost here — always a Sonnet-tier model,
# independent of OPENROUTER_MODEL (which stays Haiku for bulk extraction).
SYNTHESIS_MODEL_ENV = "SYNTHESIS_MODEL"
DEFAULT_SYNTHESIS_MODEL = "anthropic/claude-sonnet-5"


def openrouter_api_key() -> str:
    return os.environ[OPENROUTER_API_KEY_ENV]


OPENROUTER_APP_URL = "https://github.com/Shrivastav-Gaurav/ContentPilot"
OPENROUTER_APP_TITLE = "ContentPilot"


def openrouter_headers() -> dict[str, str]:
    """Headers for every OpenRouter call: auth plus app attribution (HTTP-Referer, X-Title)."""
    return {
        "Authorization": f"Bearer {openrouter_api_key()}",
        "HTTP-Referer": OPENROUTER_APP_URL,
        "X-Title": OPENROUTER_APP_TITLE,
    }


def openrouter_model() -> str:
    return os.environ.get(OPENROUTER_MODEL_ENV, DEFAULT_OPENROUTER_MODEL)


OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
EMBEDDING_MODEL = "text-embedding-3-small"


def openai_api_key() -> str | None:
    """Optional: only meaning-based search (embeddings) needs it."""
    return (os.environ.get("OPENAI_API_KEY") or "").strip() or None


def ingest_token() -> str | None:
    """Shared secret for POST /api/ingest (header X-Ingest-Token). Unset -> ingest is refused."""
    return (os.environ.get("INGEST_TOKEN") or "").strip() or None


def inbox_url() -> str:
    """Where the share page's "open Inbox" link points (the Streamlit Inbox on the same host)."""
    return os.environ.get("CONTENTPILOT_INBOX_URL", "/Inbox")


def pwa_start_url() -> str:
    """What the installed Android app opens (default: the Streamlit home page)."""
    return os.environ.get("CONTENTPILOT_PWA_START_URL", "/")


def share_requires_tailscale_identity() -> bool:
    """POST /share accepts only requests that came through `tailscale serve` from a tailnet
    user (Tailscale-User-Login header) unless this is turned off."""
    return os.environ.get("CONTENTPILOT_SHARE_REQUIRE_TS_IDENTITY", "1").strip().lower() not in ("0", "false", "no")


def jina_api_key() -> str | None:
    return os.environ.get(JINA_API_KEY_ENV) or None


def linkedin_credentials() -> tuple[str, str]:
    email = os.environ.get("LINKEDIN_EMAIL", "").strip()
    password = os.environ.get("LINKEDIN_PASSWORD", "").strip()
    if not email or not password:
        raise ValueError("LINKEDIN_EMAIL / LINKEDIN_PASSWORD not set in .env")
    return email, password


def linkedin_scan_window() -> str:
    return os.environ.get("LINKEDIN_SCAN_WINDOW", DEFAULT_LINKEDIN_SCAN_WINDOW)


def linkedin_scan_enabled() -> bool:
    return os.environ.get("LINKEDIN_SCAN_ENABLED", "").strip().lower() in ("true", "1", "yes")


def linkedin_scan_cron() -> str:
    return os.environ.get("LINKEDIN_SCAN_CRON", DEFAULT_LINKEDIN_SCAN_CRON)


def synthesis_model() -> str:
    return os.environ.get(SYNTHESIS_MODEL_ENV, DEFAULT_SYNTHESIS_MODEL)
