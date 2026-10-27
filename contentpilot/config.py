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


def openrouter_model() -> str:
    return os.environ.get(OPENROUTER_MODEL_ENV, DEFAULT_OPENROUTER_MODEL)


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
