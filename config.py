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
    OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    TAVILY_API_KEY = st.secrets.get("TAVILY_API_KEY", os.environ.get("TAVILY_API_KEY", ""))
except:
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
