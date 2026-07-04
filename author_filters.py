"""Pure helpers for author result filtering and deduplication."""

from typing import Iterable


def _clean_label(value: object) -> str:
    """Normalize labels for exact UI matching."""
    return " ".join(str(value or "").strip().split())


def author_matches_any_specialty(author: dict, selected_specialties: Iterable[str]) -> bool:
    """Case-insensitive partial match across every specialty-bearing field."""
    selected = {_clean_label(value).casefold() for value in selected_specialties if _clean_label(value)}
    if not selected:
        return True

    author_labels = {
        _clean_label(author.get(field)).casefold()
        for field in ("specialty", "subfield", "research_areas", "scientific_domain")
    }
    author_labels.update(_clean_label(topic).casefold() for topic in (author.get("all_topics") or []))
    author_labels.discard("")
    return any(term in label for term in selected for label in author_labels)


def _dedupe_key(author: dict) -> str:
    """Build the preferred author identity key for display and bulk dedupe."""
    orcid_id = _clean_label(author.get("orcid_id"))
    if orcid_id:
        return f"orcid:{orcid_id.lower()}"

    email = _clean_label(author.get("email")).lower()
    if email:
        return f"email:{email}"

    author_id = _clean_label(author.get("author_id") or author.get("openalex_id"))
    if author_id:
        return f"openalex:{author_id.rstrip('/').lower()}"

    profile_key = _clean_label(author.get("profile_key"))
    if profile_key:
        return f"profile:{profile_key.lower()}"

    return ""


def dedupe_authors(authors: Iterable[dict]) -> list[dict]:
    """Return authors deduped by ORCID, then email, then OpenAlex/profile key."""
    deduped: list[dict] = []
    seen: set[str] = set()

    for author in authors:
        key = _dedupe_key(author)
        if key:
            if key in seen:
                continue
            seen.add(key)
        deduped.append(author)

    return deduped
