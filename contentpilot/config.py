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


def openrouter_api_key() -> str:
    return os.environ[OPENROUTER_API_KEY_ENV]


def openrouter_model() -> str:
    return os.environ.get(OPENROUTER_MODEL_ENV, DEFAULT_OPENROUTER_MODEL)


def jina_api_key() -> str | None:
    return os.environ.get(JINA_API_KEY_ENV) or None
