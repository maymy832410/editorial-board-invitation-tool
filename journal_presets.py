"""Helpers for persistent journal invitation presets."""

from typing import Any, Dict


JOURNAL_PRESET_FIELDS = [
    "name",
    "issn",
    "link",
    "location",
    "editor_in_chief",
    "submission_link",
    "cite_score",
    "quartile",
    "indexing_status",
    "invitation_goal",
    "scope",
]

DEFAULT_JOURNAL_PRESET_CONFIG = {
    "name": "",
    "issn": "",
    "link": "",
    "location": "",
    "editor_in_chief": "",
    "submission_link": "",
    "cite_score": "",
    "quartile": "",
    "indexing_status": "",
    "invitation_goal": "Regular submission",
    "scope": "",
}


def normalize_journal_preset_config(config: Dict[str, Any] | None) -> Dict[str, str]:
    """Return only supported journal preset fields as strings."""
    source = config or {}
    normalized: Dict[str, str] = {}
    for field in JOURNAL_PRESET_FIELDS:
        value = source.get(field, DEFAULT_JOURNAL_PRESET_CONFIG.get(field, ""))
        normalized[field] = "" if value is None else str(value)
    if not normalized.get("invitation_goal"):
        normalized["invitation_goal"] = DEFAULT_JOURNAL_PRESET_CONFIG["invitation_goal"]
    return normalized
