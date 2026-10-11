"""Runtime configuration, read from environment variables (and an optional .env file)."""
import os
from pathlib import Path

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
FIGURE_DIR = DATA_DIR / "figures"
CARD_DIR = DATA_DIR / "cards"
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "paperagent.db"))

# --- Claude ---------------------------------------------------------------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
# Model for the cheap abstract-screening pass. Defaults to the main model;
# set e.g. SCREEN_MODEL=claude-haiku-5-5 to cut screening cost.
SCREEN_MODEL = os.environ.get("SCREEN_MODEL", CLAUDE_MODEL)
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "high")
# Server-side refusal fallback (Claude API only). Set to 0 on Bedrock/Vertex/Foundry.
CLAUDE_FALLBACKS = os.environ.get("CLAUDE_FALLBACKS", "1") == "1"

# --- Pipeline defaults (overridable from the dashboard settings page) -----
DEFAULT_INTERVAL_HOURS = _env_int("FETCH_INTERVAL_HOURS", 6)
DEFAULT_LOOKBACK_DAYS = _env_int("LOOKBACK_DAYS", 3)
DEFAULT_MAX_PER_SOURCE = _env_int("MAX_PER_SOURCE", 40)
DEFAULT_MAX_FULL_READS = _env_int("MAX_FULL_READS", 5)
DEFAULT_MIN_RELEVANCE = _env_float("MIN_RELEVANCE", 6.0)
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "1") == "1"

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
BRAND_TAGLINE = os.environ.get("BRAND_TAGLINE", "Smart Topic Analysis & Rating · Worthwise Article Research System")
# (initial letter, rest of word) pairs spelled out on the closing slide
BRAND_ACRONYM = [
    ("S", "mart"), ("T", "opic"), ("A", "nalysis &"), ("R", "ating"),
    ("W", "orthwise"), ("A", "rticle"), ("R", "esearch"), ("S", "ystem"),
]
CHROMIUM_EXECUTABLE = os.environ.get("CHROMIUM_EXECUTABLE", "")

for _d in (DATA_DIR, FIGURE_DIR, CARD_DIR):
    _d.mkdir(parents=True, exist_ok=True)
