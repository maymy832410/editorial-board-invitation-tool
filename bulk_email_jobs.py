"""Shared helpers for preparing durable bulk email jobs."""

from typing import Callable, Iterable, Optional


MAX_BULK_RECIPIENTS = 1000


def cap_bulk_recipients(authors: Iterable[dict], limit: int = MAX_BULK_RECIPIENTS) -> list[dict]:
    """Apply the hard per-job recipient cap after preserving filter order."""
    return list(authors)[:max(0, min(int(limit), MAX_BULK_RECIPIENTS))]


def prepare_bulk_recipients(
    authors: Iterable[dict],
    is_already_sent: Callable[[str], bool],
    is_suppressed: Optional[Callable[[str, str], bool]] = None,
    retracted_names: Optional[set[str]] = None,
) -> list[dict]:
    """Return unique, sendable bulk recipients from an author list."""
    retracted_lookup = retracted_names or set()
    recipients: list[dict] = []
    seen_orcids: set[str] = set()
    seen_emails: set[str] = set()

    for author in authors:
        email = (author.get("email") or "").strip()
        if not email or "@" not in email:
            continue

        author_name = (author.get("name") or author.get("author_name") or "").strip()
        if author_name.lower() in retracted_lookup:
            continue

        orcid_id = (author.get("orcid_id") or "").strip()
        normalized_email = email.lower()
        if is_suppressed and is_suppressed(email, orcid_id):
            continue
        tracking_id = orcid_id or f"email:{normalized_email}"
        if is_already_sent(tracking_id):
            continue

        if orcid_id:
            if orcid_id in seen_orcids:
                continue
            seen_orcids.add(orcid_id)
        elif normalized_email in seen_emails:
            continue

        seen_emails.add(normalized_email)
        recipients.append({**author, "email": email, "orcid_id": orcid_id})

    return recipients
