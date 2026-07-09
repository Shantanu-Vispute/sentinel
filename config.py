import os
from pathlib import Path


HERE = Path(__file__).resolve().parent


def _load_env_file(path: Path = HERE / ".env") -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


def _path_env(name: str, default: Path) -> str:
    raw = os.getenv(name)
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = HERE / path
    return str(path)


_load_env_file()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
EMBEDDING_PROVIDER = os.getenv(
    "EMBEDDING_PROVIDER", "gemini").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_API_KEYS = []
for _key in [GEMINI_API_KEY, *_csv_env("GEMINI_API_KEYS", [])]:
    if _key and _key not in GEMINI_API_KEYS:
        GEMINI_API_KEYS.append(_key)
GEMINI_EMBEDDING_MODEL = os.getenv(
    "GEMINI_EMBEDDING_MODEL", "gemini-embedding-001").strip()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e2b")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")

TEMPERATURE = _float_env("TEMPERATURE", 0.1)
LLM_TIMEOUT_SECONDS = _int_env("LLM_TIMEOUT_SECONDS", 90)
MAX_FETCH_RESULTS = _int_env("MAX_FETCH_RESULTS", 200)
SIMILARITY_THRESHOLD = _float_env("SIMILARITY_THRESHOLD", 0.80)

TELEGRAM_CHANNELS = _csv_env("TELEGRAM_CHANNELS", [])
TELEGRAM_API_ID = _int_env("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_SESSION_PATH = _path_env(
    "TELEGRAM_SESSION_PATH", HERE / "state" / "telegram"
)

SENTINEL_PII_PATTERNS = _csv_env("SENTINEL_PII_PATTERNS", [])
STORIES_DB = _path_env("STORIES_DB", HERE / "state" / "stories.db")
GMAIL_CREDENTIALS_PATH = _path_env(
    "GMAIL_CREDENTIALS_PATH", HERE / "state" / "credentials.json"
)
GMAIL_TOKEN_PATH = _path_env("GMAIL_TOKEN_PATH", HERE / "state" / "token.json")
SENDER_SIGNALS_PATH = _path_env(
    "SENDER_SIGNALS_PATH", HERE / "state" / "sender_signals.json"
)
BESTBLOGS_API_BASE = os.getenv(
    "BESTBLOGS_API_BASE",
    "https://www.bestblogs.dev/api/proxy",
).rstrip("/")
