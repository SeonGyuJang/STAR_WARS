"""Runtime configuration, read from environment variables (and an optional .env file)."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "paperagent.db"))

# Times on the dashboard and in run logs are shown in this zone (stored as UTC).
TIMEZONE = ZoneInfo(os.environ.get("DISPLAY_TIMEZONE", "Asia/Seoul"))

# Heroku sets DYNO on every dyno; its filesystem is wiped on each restart.
ON_HEROKU = "DYNO" in os.environ


def database_url() -> str:
    """DATABASE_URL (Heroku Postgres) or a local SQLite file."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return f"sqlite:///{DB_PATH}"
    # Heroku still hands out postgres:// URLs; SQLAlchemy wants an explicit driver.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


# Where card images and figures live: "local" (DATA_DIR) or "db" (blobs table).
# Defaults to "db" whenever a Postgres DATABASE_URL is set, because the dyno
# filesystem does not survive restarts.
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND") or ("db" if os.environ.get("DATABASE_URL") else "local")

# --- Claude: one model per job, priced to what the job needs ----------------
# reader  - reads the whole PDF (the big input) and writes factual English notes
# judge   - scores methodology and novelty from those notes (the decision that matters)
# writer  - turns notes + verdict into the Korean review and card copy
# screen  - rates titles/abstracts for relevance
JUDGE_MODEL = os.environ.get("JUDGE_MODEL") or os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"
READER_MODEL = os.environ.get("READER_MODEL", "claude-haiku-5-5")
WRITER_MODEL = os.environ.get("WRITER_MODEL", "claude-sonnet-5-5")
SCREEN_MODEL = os.environ.get("SCREEN_MODEL", "claude-haiku-5-5")
JUDGE_EFFORT = os.environ.get("JUDGE_EFFORT", "high")
READER_EFFORT = os.environ.get("READER_EFFORT", "low")
WRITER_EFFORT = os.environ.get("WRITER_EFFORT", "low")
# Server-side refusal fallback (Claude API only; Opus/Sonnet/Fable). Set 0 on Bedrock/Vertex/Foundry.
CLAUDE_FALLBACKS = os.environ.get("CLAUDE_FALLBACKS", "1") == "1"

# USD per million tokens (input, output), for the cost shown on the dashboard.
# Haiku 5.5 has a higher rate card for prompts over 100K tokens.
MODEL_PRICES = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
HAIKU_LONG_PROMPT_PRICES = (0.50, 2.50)

# --- Pipeline defaults (overridable from the dashboard settings page) -----
DEFAULT_INTERVAL_HOURS = _env_int("FETCH_INTERVAL_HOURS", 12)
DEFAULT_LOOKBACK_DAYS = _env_int("LOOKBACK_DAYS", 3)
DEFAULT_MAX_PER_SOURCE = _env_int("MAX_PER_SOURCE", 40)
DEFAULT_MAX_FULL_READS = _env_int("MAX_FULL_READS", 3)
DEFAULT_MIN_RELEVANCE = _env_float("MIN_RELEVANCE", 7.0)
# In-process scheduler. Off by default on Heroku, where the Heroku Scheduler
# add-on runs `python -m paperagent run --if-due` instead.
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "0" if ON_HEROKU else "1") == "1"

# --- External services ----------------------------------------------------
# OpenAlex asks for a contact address to use its "polite pool".
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "")
SEMANTIC_SCHOLAR_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
HTTP_USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT", "STARWARS-Research/1.0 (paper digest bot; +https://github.com/SeonGyuJang/STAR_WARS)"
)

# --- Dashboard ------------------------------------------------------------
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me")
# When set, adding/removing keywords & journals and manual runs require login.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SITE_NAME = os.environ.get("SITE_NAME", "STAR WARS")

# --- Brand / card news ------------------------------------------------------
# STAR WARS = Smart Topic Analysis & Rating : Worthwise Article Research System
CARD_BRAND = os.environ.get("CARD_BRAND", "STAR WARS")
CARD_HANDLE = os.environ.get("CARD_HANDLE", "@starwars.research")
# Instagram allows 5 hashtags per post (since Dec 2025). The fixed, widely
# searched ones come first; the AI adds tags for the paper's own field.
CARD_HASHTAG_LIMIT = int(os.environ.get("CARD_HASHTAG_LIMIT", "5"))
CARD_BASE_HASHTAGS = [t.strip() for t in os.environ.get("CARD_BASE_HASHTAGS", "#인공지능,#AI,#논문리뷰").split(",") if t.strip()]
BRAND_TAGLINE = os.environ.get("BRAND_TAGLINE", "Smart Topic Analysis & Rating · Worthwise Article Research System")
# (initial letter, rest of word) pairs spelled out on the closing slide
BRAND_ACRONYM = [
    ("S", "mart"), ("T", "opic"), ("A", "nalysis &"), ("R", "ating"),
    ("W", "orthwise"), ("A", "rticle"), ("R", "esearch"), ("S", "ystem"),
]
CHROMIUM_EXECUTABLE = os.environ.get("CHROMIUM_EXECUTABLE", "")

DATA_DIR.mkdir(parents=True, exist_ok=True)
