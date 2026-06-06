"""Configuration and constants for the Author Email Finder app."""

# Country codes (ISO 3166-1 alpha-2)
COUNTRIES = {
    "United States": "US",
    "United Kingdom": "GB",
    "Germany": "DE",
    "France": "FR",
    "China": "CN",
    "Japan": "JP",
    "Canada": "CA",
    "Australia": "AU",
    "India": "IN",
    "Brazil": "BR",
    "Italy": "IT",
    "Spain": "ES",
    "Netherlands": "NL",
    "Switzerland": "CH",
    "South Korea": "KR",
    "Sweden": "SE",
    "Russia": "RU",
    "Singapore": "SG",
    "Israel": "IL",
    "Belgium": "BE",
    "Austria": "AT",
    "Denmark": "DK",
    "Norway": "NO",
    "Finland": "FI",
    "Poland": "PL",
    "Portugal": "PT",
    "Ireland": "IE",
    "New Zealand": "NZ",
    "Mexico": "MX",
    "Argentina": "AR",
    "Saudi Arabia": "SA",
    "United Arab Emirates": "AE",
    "Turkey": "TR",
    "South Africa": "ZA",
    "Egypt": "EG",
    "Malaysia": "MY",
    "Thailand": "TH",
    "Indonesia": "ID",
    "Taiwan": "TW",
    "Hong Kong": "HK",
}

# OpenAlex API settings
OPENALEX_BASE_URL = "https://api.openalex.org"
OPENALEX_EMAIL = "maymy832410@gmail.com"  # For polite pool access

# ORCID API settings
ORCID_API_BASE_URL = "https://pub.orcid.org/v3.0"

# OpenAI API settings (for email extraction)
# Read from environment variable or Streamlit secrets
import os
try:
    import streamlit as st
    # st.secrets doesn't have .get() method - use 'in' check instead
    if hasattr(st, 'secrets') and "OPENAI_API_KEY" in st.secrets:
        OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
    else:
        OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    
    if hasattr(st, 'secrets') and "TAVILY_API_KEY" in st.secrets:
        TAVILY_API_KEY = st.secrets["TAVILY_API_KEY"]
    else:
        TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
except Exception:
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# Default settings
DEFAULT_H_INDEX_MIN = 10
DEFAULT_H_INDEX_MAX = 50
DEFAULT_DELAY_SECONDS = 3
DEFAULT_MAX_RESULTS = 1000

# File paths
DATA_DIR = "data"
PROGRESS_FILE = "progress.json"


def _env_int(name: str, default: int) -> int:
    """Read an int environment variable with a fallback default."""
    raw = (os.environ.get(name, "") or "").strip()
    try:
        return int(raw) if raw else int(default)
    except ValueError:
        return int(default)


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable with a fallback default."""
    raw = (os.environ.get(name, "") or "").strip()
    try:
        return float(raw) if raw else float(default)
    except ValueError:
        return float(default)


# Background email-collection worker settings (env-tunable)
COLLECT_BASELINE_CONCURRENCY = _env_int("COLLECT_BASELINE_CONCURRENCY", 2)
COLLECT_BASELINE_DELAY_SEC = _env_float("COLLECT_BASELINE_DELAY_SEC", 3.0)
COLLECT_COOLDOWN_MIN = _env_int("COLLECT_COOLDOWN_MIN", 20)
COLLECT_COOLDOWN_DELAY_MIN = _env_float("COLLECT_COOLDOWN_DELAY_MIN", 8.0)
COLLECT_COOLDOWN_DELAY_MAX = _env_float("COLLECT_COOLDOWN_DELAY_MAX", 12.0)
COLLECT_RECOVERY_STEP = _env_int("COLLECT_RECOVERY_STEP", 2)
COLLECT_SEED_MIN_QUEUE = _env_int("COLLECT_SEED_MIN_QUEUE", 2000)
COLLECT_SEED_BATCH_SIZE = _env_int("COLLECT_SEED_BATCH_SIZE", 200)
COLLECT_FETCH_BATCH = _env_int("COLLECT_FETCH_BATCH", 20)
COLLECT_NO_EMAIL_RETRY_DAYS = _env_int("COLLECT_NO_EMAIL_RETRY_DAYS", 30)
COLLECT_ERROR_RETRY_HOURS = _env_int("COLLECT_ERROR_RETRY_HOURS", 6)
COLLECT_CYCLE_PAUSE_SEC = _env_float("COLLECT_CYCLE_PAUSE_SEC", 3.0)
COLLECT_IDLE_POLL_SEC = _env_int("COLLECT_IDLE_POLL_SEC", 30)
