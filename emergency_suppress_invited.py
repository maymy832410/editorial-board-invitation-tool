"""Emergency suppress-and-purge job for all currently invited recipients.

The script is intentionally dry-run by default. It only mutates production data
when run with the exact confirmation flag:

    python emergency_suppress_invited.py --confirm SUPPRESS_ALL_INVITED
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from db_client import (
    BULK_RECIPIENT_STATUS_PENDING,
    BULK_RECIPIENT_STATUS_SENDING,
    PostgresStorage,
    get_storage,
)


CONFIRMATION_TEXT = "SUPPRESS_ALL_INVITED"
SUPPRESSION_REASON = "Emergency suppression of all current invited recipients"
SUPPRESSION_SOURCE = "emergency_all_invited"


def normalize_email(email: Any) -> str:
    """Return a lowercase email or an empty string for invalid values."""
    value = str(email or "").strip().lower()
    return value if "@" in value else ""


def domain_for(email: str) -> str:
    """Return the domain part for safe aggregate reporting."""
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


@dataclass
class InvitedEmailInventory:
    emails: set[str] = field(default_factory=set)
    source_counts: Counter = field(default_factory=Counter)
    domain_counts: Counter = field(default_factory=Counter)
    bulk_job_ids: set[int] = field(default_factory=set)
    bulk_pending_or_sending: int = 0

    def add(self, email: str, source: str, job_id: int | None = None, status: str = "") -> None:
        normalized = normalize_email(email)
        if not normalized:
            return
        self.emails.add(normalized)
        self.source_counts[source] += 1
        domain = domain_for(normalized)
        if domain:
            self.domain_counts[domain] += 1
        if job_id is not None:
            self.bulk_job_ids.add(int(job_id))
        if source == "bulk_email_recipients" and status in {BULK_RECIPIENT_STATUS_PENDING, BULK_RECIPIENT_STATUS_SENDING}:
            self.bulk_pending_or_sending += 1


def _fetch_rows(cur: Any, query: str) -> Iterable[dict[str, Any]]:
    cur.execute(query)
    return [dict(row) for row in cur.fetchall()]


def collect_invited_emails(storage: PostgresStorage) -> InvitedEmailInventory:
    """Collect normalized emails from all current invitation evidence tables."""
    inventory = InvitedEmailInventory()
    with storage._get_cursor() as cur:
        for row in _fetch_rows(
            cur,
            f"""
            SELECT email
            FROM {storage.INVITATION_TABLE_NAME}
            WHERE email IS NOT NULL AND email <> '';
            """,
        ):
            inventory.add(row.get("email", ""), "author_invitations")

        for row in _fetch_rows(
            cur,
            f"""
            SELECT email
            FROM {storage.TABLE_NAME}
            WHERE email IS NOT NULL AND email <> '';
            """,
        ):
            inventory.add(row.get("email", ""), "sent_invitations")

        for row in _fetch_rows(
            cur,
            f"""
            SELECT email, job_id, status
            FROM {storage.BULK_EMAIL_RECIPIENTS_TABLE}
            WHERE email IS NOT NULL AND email <> '';
            """,
        ):
            inventory.add(
                row.get("email", ""),
                "bulk_email_recipients",
                job_id=row.get("job_id"),
                status=row.get("status", ""),
            )

        for row in _fetch_rows(
            cur,
            f"""
            SELECT email
            FROM {storage.PROFILE_TABLE_NAME}
            WHERE email IS NOT NULL
              AND email <> ''
              AND (
                  COALESCE(invitation_count_total, 0) > 0
                  OR COALESCE(invitation_count_editorial, 0) > 0
                  OR COALESCE(invitation_count_publication, 0) > 0
                  OR last_invited_at IS NOT NULL
              );
            """,
        ):
            inventory.add(row.get("email", ""), "author_profiles_invited")

    return inventory


def count_already_suppressed(storage: PostgresStorage, emails: Iterable[str]) -> int:
    """Count how many emails already have active suppression tombstones."""
    return sum(1 for email in emails if storage.is_email_suppressed(email))


def suppress_inventory(storage: PostgresStorage, inventory: InvitedEmailInventory) -> dict[str, Any]:
    """Suppress every email in the inventory using the shared purge path."""
    already_suppressed = 0
    newly_suppressed = 0
    failed: list[str] = []

    for email in sorted(inventory.emails):
        was_suppressed = storage.is_email_suppressed(email)
        result = storage.suppress_recipient(
            email=email,
            reason=SUPPRESSION_REASON,
            source=SUPPRESSION_SOURCE,
        )
        if not result:
            failed.append(email)
            continue
        if was_suppressed:
            already_suppressed += 1
        else:
            newly_suppressed += 1

    return {
        "already_suppressed": already_suppressed,
        "newly_suppressed": newly_suppressed,
        "failed_count": len(failed),
        "failed_domains": dict(Counter(domain_for(email) for email in failed if domain_for(email)).most_common(10)),
    }


def verify_no_unsuppressed_invited_emails(storage: PostgresStorage) -> dict[str, Any]:
    """Return aggregate counts of invited-source emails that are not actively suppressed."""
    checks = {
        "author_invitations": f"""
            SELECT COUNT(*) AS cnt
            FROM {storage.INVITATION_TABLE_NAME} i
            LEFT JOIN {storage.EMAIL_SUPPRESSIONS_TABLE} s
              ON s.email_lower = LOWER(i.email) AND s.is_suppressed = TRUE
            WHERE i.email IS NOT NULL AND i.email <> '' AND s.id IS NULL;
        """,
        "sent_invitations": f"""
            SELECT COUNT(*) AS cnt
            FROM {storage.TABLE_NAME} i
            LEFT JOIN {storage.EMAIL_SUPPRESSIONS_TABLE} s
              ON s.email_lower = LOWER(i.email) AND s.is_suppressed = TRUE
            WHERE i.email IS NOT NULL AND i.email <> '' AND s.id IS NULL;
        """,
        "bulk_pending_or_sending": f"""
            SELECT COUNT(*) AS cnt
            FROM {storage.BULK_EMAIL_RECIPIENTS_TABLE} r
            LEFT JOIN {storage.EMAIL_SUPPRESSIONS_TABLE} s
              ON s.email_lower = LOWER(r.email) AND s.is_suppressed = TRUE
            WHERE r.email IS NOT NULL
              AND r.email <> ''
              AND r.status IN ('{BULK_RECIPIENT_STATUS_PENDING}', '{BULK_RECIPIENT_STATUS_SENDING}')
              AND s.id IS NULL;
        """,
        "author_profiles_invited": f"""
            SELECT COUNT(*) AS cnt
            FROM {storage.PROFILE_TABLE_NAME} p
            LEFT JOIN {storage.EMAIL_SUPPRESSIONS_TABLE} s
              ON s.email_lower = p.email_lower AND s.is_suppressed = TRUE
            WHERE p.email IS NOT NULL
              AND p.email <> ''
              AND (
                  COALESCE(p.invitation_count_total, 0) > 0
                  OR COALESCE(p.invitation_count_editorial, 0) > 0
                  OR COALESCE(p.invitation_count_publication, 0) > 0
                  OR p.last_invited_at IS NOT NULL
              )
              AND s.id IS NULL;
        """,
    }
    with storage._get_cursor() as cur:
        counts = {}
        for name, query in checks.items():
            cur.execute(query)
            row = cur.fetchone() or {}
            counts[name] = int(row.get("cnt") or 0)
    counts["total_remaining"] = sum(counts.values())
    return counts


def build_safe_inventory_summary(storage: PostgresStorage, inventory: InvitedEmailInventory) -> dict[str, Any]:
    """Build non-PII summary details for dry-run and confirmed outputs."""
    return {
        "discovered_unique_emails": len(inventory.emails),
        "source_rows_seen": dict(inventory.source_counts),
        "already_suppressed": count_already_suppressed(storage, inventory.emails),
        "bulk_job_count_seen": len(inventory.bulk_job_ids),
        "bulk_pending_or_sending_rows_seen": inventory.bulk_pending_or_sending,
        "top_domains": dict(inventory.domain_counts.most_common(15)),
    }


def run(argv: list[str] | None = None, storage: PostgresStorage | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emergency suppress all currently invited recipients.")
    parser.add_argument("--dry-run", action="store_true", help="Preview aggregate counts without mutation.")
    parser.add_argument("--confirm", default="", help=f"Required exact text for mutation: {CONFIRMATION_TEXT}")
    parser.add_argument("--verify", action="store_true", help="Run post-operation aggregate verification only.")
    args = parser.parse_args(argv)

    storage = storage or get_storage()
    if not storage.available:
        print(json.dumps({"ok": False, "error": storage.error_message or "Database unavailable"}, indent=2))
        return 2

    if args.verify:
        verification = verify_no_unsuppressed_invited_emails(storage)
        print(json.dumps({"ok": verification["total_remaining"] == 0, "verification": verification}, indent=2))
        return 0 if verification["total_remaining"] == 0 else 1

    inventory = collect_invited_emails(storage)
    summary = build_safe_inventory_summary(storage, inventory)

    if args.confirm != CONFIRMATION_TEXT:
        print(json.dumps({
            "ok": True,
            "mode": "dry_run",
            "summary": summary,
            "message": f"No changes made. Re-run with --confirm {CONFIRMATION_TEXT} to suppress and purge.",
        }, indent=2))
        return 0

    mutation = suppress_inventory(storage, inventory)
    verification = verify_no_unsuppressed_invited_emails(storage)
    ok = mutation["failed_count"] == 0 and verification["total_remaining"] == 0
    print(json.dumps({
        "ok": ok,
        "mode": "confirmed",
        "summary": summary,
        "mutation": mutation,
        "verification": verification,
    }, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
