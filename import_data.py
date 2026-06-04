"""One-time script to import sent_invitations CSV and retraction_watch.csv into Railway PostgreSQL.

Usage:
    railway run python import_data.py          # on Railway
    DATABASE_URL=... python import_data.py     # locally with env var
"""

import csv
import os
import sys
import time
from typing import Any, Optional, cast
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

from db_client import (
    OPENALEX_MATCH_STATUS_MATCHED,
    OPENALEX_MATCH_STATUS_PENDING_MANUAL,
    get_storage,
)
from openalex_client import OpenAlexClient, OpenAlexRequestError


def extract_email_domain(email: str) -> str:
    """Extract a normalized email domain."""
    normalized = (email or "").strip().lower()
    if "@" not in normalized:
        return ""
    domain = normalized.rsplit("@", 1)[-1].strip()
    return domain if "." in domain else ""


def get_conn() -> Any:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)
    parsed = urlparse(database_url)
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
        sslmode="disable",
    )
    conn.autocommit = True
    return conn


def ensure_tables(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sent_invitations (
                orcid_id    TEXT PRIMARY KEY,
                author_name TEXT DEFAULT '',
                email       TEXT DEFAULT '',
                publisher   TEXT DEFAULT '',
                sent_at     TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            ALTER TABLE sent_invitations
            ADD COLUMN IF NOT EXISTS email_domain TEXT DEFAULT '';
        """)
        cur.execute("""
            ALTER TABLE sent_invitations
            ADD COLUMN IF NOT EXISTS openalex_id TEXT DEFAULT '';
        """)
        cur.execute("""
            ALTER TABLE sent_invitations
            ADD COLUMN IF NOT EXISTS scientific_domain TEXT DEFAULT '';
        """)
        cur.execute("""
            ALTER TABLE sent_invitations
            ADD COLUMN IF NOT EXISTS invitation_count INTEGER DEFAULT 0;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS author_invitations (
                id              SERIAL PRIMARY KEY,
                orcid_id        TEXT NOT NULL,
                invitation_type TEXT NOT NULL DEFAULT 'editorial',
                author_name     TEXT DEFAULT '',
                email           TEXT DEFAULT '',
                email_domain    TEXT DEFAULT '',
                publisher       TEXT DEFAULT '',
                journal_name    TEXT DEFAULT '',
                template_id     TEXT DEFAULT '',
                cite_score      TEXT DEFAULT '',
                quartile        TEXT DEFAULT '',
                sent_at         TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_author_invitations_unique
            ON author_invitations (orcid_id, invitation_type, journal_name);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_author_invitations_type
            ON author_invitations (invitation_type);
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS author_profiles (
                profile_key                  TEXT PRIMARY KEY,
                orcid_id                     TEXT DEFAULT '',
                openalex_id                  TEXT DEFAULT '',
                author_name                  TEXT DEFAULT '',
                author_name_lower            TEXT DEFAULT '',
                email                        TEXT DEFAULT '',
                email_lower                  TEXT DEFAULT '',
                email_domain                 TEXT DEFAULT '',
                scientific_domain            TEXT DEFAULT '',
                scientific_domains_json      TEXT DEFAULT '[]',
                openalex_match_status        TEXT DEFAULT 'unknown',
                openalex_match_confidence    DOUBLE PRECISION DEFAULT 0,
                invitation_count_total       INTEGER DEFAULT 0,
                invitation_count_editorial   INTEGER DEFAULT 0,
                invitation_count_publication INTEGER DEFAULT 0,
                last_invited_at              TIMESTAMPTZ,
                cooldown_until               TIMESTAMPTZ,
                publisher                    TEXT DEFAULT '',
                source                       TEXT DEFAULT '',
                created_at                   TIMESTAMPTZ DEFAULT NOW(),
                updated_at                   TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_author_profiles_email_lower
            ON author_profiles (email_lower);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_author_profiles_orcid
            ON author_profiles (orcid_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_author_profiles_openalex
            ON author_profiles (openalex_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_author_profiles_match_status
            ON author_profiles (openalex_match_status);
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS retracted_authors (
                id              SERIAL PRIMARY KEY,
                author_name     TEXT NOT NULL,
                author_name_lower TEXT NOT NULL,
                record_id       TEXT DEFAULT '',
                journal         TEXT DEFAULT '',
                publisher       TEXT DEFAULT '',
                retraction_date TEXT DEFAULT '',
                reason          TEXT DEFAULT ''
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_retracted_name_lower
            ON retracted_authors (author_name_lower);
        """)
    print("Tables ensured.")


def import_sent_invitations(conn: Any, csv_path: str = "sent_invitations_rows(1).csv") -> None:
    if not os.path.exists(csv_path):
        print(f"SKIP: {csv_path} not found.")
        return

    rows: list[tuple[str, str, str, str, str, Optional[str], int]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            orcid_id = row.get("orcid_id", "").strip()
            if not orcid_id:
                continue
            email = row.get("email", "").strip()
            rows.append((
                orcid_id,
                row.get("author_name", "").strip(),
                email,
                extract_email_domain(email),
                row.get("publisher", "").strip(),
                row.get("sent_at", "").strip() or None,
                1,
            ))

    if not rows:
        print("No rows found in sent_invitations CSV.")
        return

    print(f"Importing {len(rows)} sent invitations...")
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(  # type: ignore[reportUnknownMemberType]
            cur,
            """
            INSERT INTO sent_invitations
                (orcid_id, author_name, email, email_domain, publisher, sent_at, invitation_count)
            VALUES %s
            ON CONFLICT (orcid_id) DO UPDATE SET
                author_name = EXCLUDED.author_name,
                email = EXCLUDED.email,
                email_domain = EXCLUDED.email_domain,
                publisher = EXCLUDED.publisher,
                sent_at = EXCLUDED.sent_at,
                invitation_count = GREATEST(sent_invitations.invitation_count, EXCLUDED.invitation_count);
            """,
            rows,
            page_size=1000,
        )
        typed_rows: list[tuple[str, str, str, str, str, str, str, str, str, str, Optional[str]]] = [
            (orcid_id, "editorial", author_name, email, email_domain, publisher, "", "", "", "", sent_at)
            for orcid_id, author_name, email, email_domain, publisher, sent_at, _invitation_count in rows
        ]
        psycopg2.extras.execute_values(  # type: ignore[reportUnknownMemberType]
            cur,
            """
            INSERT INTO author_invitations
                (orcid_id, invitation_type, author_name, email, email_domain, publisher, journal_name,
                 template_id, cite_score, quartile, sent_at)
            VALUES %s
            ON CONFLICT (orcid_id, invitation_type, journal_name) DO NOTHING;
            """,
            typed_rows,
            page_size=1000,
        )
    # Verify
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sent_invitations;")
        count = cur.fetchone()[0]
    print(f"Done. sent_invitations table now has {count} rows.")


def import_retraction_watch(conn: Any, csv_path: str = "retraction_watch.csv") -> None:
    if not os.path.exists(csv_path):
        print(f"SKIP: {csv_path} not found.")
        return

    # Check if already imported
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM retracted_authors;")
        existing = cur.fetchone()[0]
    if existing > 0:
        print(f"retracted_authors already has {existing} rows. Skipping import (delete table to re-import).")
        return

    print("Reading retraction_watch.csv...")
    batch: list[tuple[str, str, str, str, str, str, str]] = []
    total = 0
    batch_size = 5000

    with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            authors_str = row.get("Author", "")
            if not authors_str:
                continue
            record_id = row.get("Record ID", "").strip()
            journal = row.get("Journal", "").strip()
            publisher = row.get("Publisher", "").strip()
            retraction_date = row.get("RetractionDate", "").strip()
            reason = row.get("Reason", "").strip()

            for name in authors_str.split(";"):
                name = name.strip()
                if not name:
                    continue
                batch.append((
                    name,
                    name.lower(),
                    record_id,
                    journal,
                    publisher,
                    retraction_date,
                    reason,
                ))

            if len(batch) >= batch_size:
                _insert_retraction_batch(conn, batch)
                total += len(batch)
                print(f"  ...inserted {total} author entries so far")
                batch = []

    if batch:
        _insert_retraction_batch(conn, batch)
        total += len(batch)

    # Verify
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM retracted_authors;")
        count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT author_name_lower) FROM retracted_authors;")
        unique = cur.fetchone()[0]
    print(f"Done. retracted_authors: {count} rows, {unique} unique author names.")


def _insert_retraction_batch(conn: Any, batch: list[tuple[str, str, str, str, str, str, str]]) -> None:
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(  # type: ignore[reportUnknownMemberType]
            cur,
            """
            INSERT INTO retracted_authors
                (author_name, author_name_lower, record_id, journal, publisher, retraction_date, reason)
            VALUES %s;
            """,
            batch,
            page_size=2000,
        )


def backfill_profiles_and_counters(max_rows: Optional[int] = None) -> None:
    """Use db_client profile methods to seed profile rows and invitation counters."""
    storage = get_storage()
    if not storage.available:
        print("SKIP: storage unavailable; profile backfill was not executed.")
        return

    stats = storage.backfill_author_profiles(max_rows=max_rows)
    print(
        "Profile backfill complete:",
        f"seeded={stats.get('seeded_profiles', 0)},",
        f"counter_snapshots={stats.get('counter_snapshots', 0)},",
        f"pending_manual={stats.get('pending_manual_marked', 0)}",
    )


def _read_env_int(name: str, default: int, minimum: int = 0) -> int:
    """Read an integer environment variable with fallback and clamping."""
    raw_value = (os.environ.get(name, "") or "").strip()
    if not raw_value:
        return max(int(default), minimum)
    try:
        return max(int(raw_value), minimum)
    except ValueError:
        print(f"WARN: invalid {name}={raw_value!r}; using {default}")
        return max(int(default), minimum)


def _read_env_float(name: str, default: float, minimum: float = 0.0) -> float:
    """Read a float environment variable with fallback and clamping."""
    raw_value = (os.environ.get(name, "") or "").strip()
    if not raw_value:
        return max(float(default), minimum)
    try:
        return max(float(raw_value), minimum)
    except ValueError:
        print(f"WARN: invalid {name}={raw_value!r}; using {default}")
        return max(float(default), minimum)


def _read_env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable with common truthy/falsy forms."""
    raw_value = (os.environ.get(name, "") or "").strip().lower()
    if not raw_value:
        return bool(default)
    if raw_value in {"1", "true", "yes", "y", "on"}:
        return True
    if raw_value in {"0", "false", "no", "n", "off"}:
        return False
    print(f"WARN: invalid {name}={raw_value!r}; using {default}")
    return bool(default)


def enrich_profiles_from_openalex(conn: Any, limit: int = 500) -> None:
    """Enrich profile rows via strict ORCID -> OpenAlex lookup using chunked looping."""
    storage = get_storage()
    if not storage.available:
        print("SKIP: storage unavailable; OpenAlex enrichment was not executed.")
        return

    batch_size = _read_env_int("OPENALEX_ENRICH_BATCH_SIZE", max(int(limit or 500), 1), minimum=1)
    max_total = _read_env_int("OPENALEX_ENRICH_MAX_TOTAL", 0, minimum=0)
    progress_every = _read_env_int("OPENALEX_ENRICH_PROGRESS_EVERY", 50, minimum=1)
    batch_pause_seconds = _read_env_float("OPENALEX_ENRICH_BATCH_PAUSE_SEC", 0.0, minimum=0.0)
    include_pending_manual = _read_env_bool("OPENALEX_ENRICH_INCLUDE_PENDING_MANUAL", default=False)
    max_deferred_errors = _read_env_int("OPENALEX_ENRICH_MAX_DEFERRED_ERRORS", 0, minimum=0)

    initial_remaining = storage.count_profiles_needing_openalex(
        require_orcid=True,
        include_pending_manual=include_pending_manual,
    )
    if initial_remaining <= 0:
        print("No ORCID profiles need OpenAlex enrichment.")
        return

    effective_max_total = max_total
    if include_pending_manual and effective_max_total <= 0:
        # Pending-manual rows remain eligible after failed retries, so cap one pass by default.
        effective_max_total = initial_remaining
        print(
            "OPENALEX_ENRICH_INCLUDE_PENDING_MANUAL enabled without OPENALEX_ENRICH_MAX_TOTAL; "
            f"limiting this run to {effective_max_total} attempts to avoid infinite retries."
        )

    client = OpenAlexClient()

    total_attempted = 0
    total_matched = 0
    total_pending_manual = 0
    total_deferred = 0
    batch_index = 0
    stop_due_deferred_limit = False
    deferred_profile_keys: set[str] = set()

    print(
        "Starting OpenAlex enrichment:",
        f"initial_queue={initial_remaining},",
        f"batch_size={batch_size},",
        f"max_total={effective_max_total if effective_max_total > 0 else 'unbounded'},",
        f"include_pending_manual={include_pending_manual},",
        f"max_deferred_errors={max_deferred_errors if max_deferred_errors > 0 else 'unbounded'}",
    )

    while True:
        if effective_max_total > 0 and total_attempted >= effective_max_total:
            print(f"Reached OPENALEX_ENRICH_MAX_TOTAL={effective_max_total}; stopping early.")
            break

        limit_for_batch = batch_size
        if effective_max_total > 0:
            limit_for_batch = min(limit_for_batch, effective_max_total - total_attempted)
            if limit_for_batch <= 0:
                break

        candidates = storage.get_profiles_needing_openalex(
            limit=limit_for_batch,
            require_orcid=True,
            include_pending_manual=include_pending_manual,
        )
        if deferred_profile_keys:
            candidates = [
                profile
                for profile in candidates
                if (profile.get("profile_key") or "") not in deferred_profile_keys
            ]
        if not candidates:
            if deferred_profile_keys:
                print(
                    "No retry-eligible profiles left in this run; "
                    "deferred rows remain queued for the next run."
                )
            break

        batch_index += 1
        batch_attempted = 0
        batch_matched = 0
        batch_pending_manual = 0
        batch_deferred = 0

        print(f"Batch {batch_index}: processing {len(candidates)} profiles")
        for profile in candidates:
            profile_key = profile.get("profile_key", "")
            orcid_id = profile.get("orcid_id", "")
            if not profile_key or not orcid_id:
                continue

            total_attempted += 1
            batch_attempted += 1

            try:
                author = client.fetch_author_by_orcid(orcid_id)
            except OpenAlexRequestError as exc:
                deferred_profile_keys.add(profile_key)
                total_deferred += 1
                batch_deferred += 1
                if batch_deferred <= 3:
                    print(f"  deferred (request error) profile_key={profile_key}: {exc}")
                if max_deferred_errors > 0 and total_deferred >= max_deferred_errors:
                    print(
                        f"Reached OPENALEX_ENRICH_MAX_DEFERRED_ERRORS={max_deferred_errors}; "
                        "stopping early."
                    )
                    stop_due_deferred_limit = True
                    break
                continue
            except Exception as exc:
                deferred_profile_keys.add(profile_key)
                total_deferred += 1
                batch_deferred += 1
                if batch_deferred <= 3:
                    print(f"  deferred (unexpected error) profile_key={profile_key}: {exc}")
                if max_deferred_errors > 0 and total_deferred >= max_deferred_errors:
                    print(
                        f"Reached OPENALEX_ENRICH_MAX_DEFERRED_ERRORS={max_deferred_errors}; "
                        "stopping early."
                    )
                    stop_due_deferred_limit = True
                    break
                continue

            if author:
                openalex_id = author.get("author_id", "") or ""
                scientific_domain = author.get("discipline", "") or ""
                topics_payload = author.get("all_topics")
                raw_topics = cast(list[str], topics_payload) if isinstance(topics_payload, list) else []
                domains = [topic for topic in raw_topics if topic][:8]
                updated = storage.update_profile_openalex(
                    profile_key=profile_key,
                    openalex_id=openalex_id,
                    scientific_domain=scientific_domain,
                    scientific_domains=domains,
                    match_status=OPENALEX_MATCH_STATUS_MATCHED,
                    match_confidence=1.0,
                )
                if updated:
                    total_matched += 1
                    batch_matched += 1
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE sent_invitations
                            SET openalex_id = %s,
                                scientific_domain = %s
                            WHERE orcid_id = %s;
                            """,
                            (openalex_id, scientific_domain, orcid_id),
                        )
            else:
                updated = storage.update_profile_openalex(
                    profile_key=profile_key,
                    openalex_id="",
                    scientific_domain="",
                    scientific_domains=[],
                    match_status=OPENALEX_MATCH_STATUS_PENDING_MANUAL,
                    match_confidence=0.0,
                )
                if updated:
                    total_pending_manual += 1
                    batch_pending_manual += 1

            if total_attempted % progress_every == 0:
                print(
                    f"  progress: attempted={total_attempted},",
                    f"matched={total_matched},",
                    f"pending_manual={total_pending_manual},",
                    f"deferred={total_deferred}",
                )

        remaining = storage.count_profiles_needing_openalex(
            require_orcid=True,
            include_pending_manual=include_pending_manual,
        )
        request_stats = client.request_stats
        print(
            f"Batch {batch_index} complete:",
            f"attempted={batch_attempted},",
            f"matched={batch_matched},",
            f"pending_manual={batch_pending_manual},",
            f"deferred={batch_deferred},",
            f"remaining={remaining},",
            f"api_requests={request_stats.get('requests', 0)},",
            f"api_429={request_stats.get('rate_limited', 0)},",
            f"api_5xx={request_stats.get('server_errors', 0)},",
            f"api_network={request_stats.get('network_errors', 0)},",
            f"retry_exhausted={request_stats.get('retry_exhausted', 0)}",
        )

        if stop_due_deferred_limit:
            break
        if remaining <= 0:
            break
        if batch_pause_seconds > 0:
            time.sleep(batch_pause_seconds)

    final_remaining = storage.count_profiles_needing_openalex(
        require_orcid=True,
        include_pending_manual=include_pending_manual,
    )
    request_stats = client.request_stats
    print(
        "OpenAlex enrichment complete:",
        f"attempted={total_attempted},",
        f"matched={total_matched},",
        f"pending_manual={total_pending_manual},",
        f"deferred={total_deferred},",
        f"remaining={final_remaining},",
        f"api_requests={request_stats.get('requests', 0)},",
        f"api_429={request_stats.get('rate_limited', 0)},",
        f"api_5xx={request_stats.get('server_errors', 0)},",
        f"api_network={request_stats.get('network_errors', 0)},",
        f"retry_exhausted={request_stats.get('retry_exhausted', 0)}",
    )


if __name__ == "__main__":
    conn = get_conn()
    ensure_tables(conn)
    import_sent_invitations(conn)
    backfill_profiles_and_counters()
    legacy_enrich_limit = _read_env_int("OPENALEX_ENRICH_LIMIT", 500, minimum=1)
    enrich_batch_size = _read_env_int("OPENALEX_ENRICH_BATCH_SIZE", legacy_enrich_limit, minimum=1)
    enrich_profiles_from_openalex(conn, limit=enrich_batch_size)
    import_retraction_watch(conn)
    conn.close()
    print("\nAll imports complete.")
