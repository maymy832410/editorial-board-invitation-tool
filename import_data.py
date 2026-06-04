"""One-time script to import sent_invitations CSV and retraction_watch.csv into Railway PostgreSQL.

Usage:
    railway run python import_data.py          # on Railway
    DATABASE_URL=... python import_data.py     # locally with env var
"""

import csv
import os
import sys
from typing import Any, Optional, cast
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

from db_client import (
    OPENALEX_MATCH_STATUS_MATCHED,
    OPENALEX_MATCH_STATUS_PENDING_MANUAL,
    get_storage,
)
from openalex_client import OpenAlexClient


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


def enrich_profiles_from_openalex(conn: Any, limit: int = 500) -> None:
    """Enrich profile rows via strict ORCID -> OpenAlex lookup only."""
    storage = get_storage()
    if not storage.available:
        print("SKIP: storage unavailable; OpenAlex enrichment was not executed.")
        return

    candidates = storage.get_profiles_needing_openalex(limit=limit, require_orcid=True)
    if not candidates:
        print("No ORCID profiles need OpenAlex enrichment.")
        return

    client = OpenAlexClient()
    matched = 0
    pending_manual = 0

    print(f"Enriching up to {len(candidates)} ORCID profiles from OpenAlex...")
    for index, profile in enumerate(candidates, start=1):
        profile_key = profile.get("profile_key", "")
        orcid_id = profile.get("orcid_id", "")
        if not profile_key or not orcid_id:
            continue

        author = client.fetch_author_by_orcid(orcid_id)
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
                matched += 1
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
                pending_manual += 1

        if index % 50 == 0:
            print(f"  processed {index}/{len(candidates)} profiles")

    print(
        "OpenAlex enrichment complete:",
        f"matched={matched},",
        f"pending_manual={pending_manual}",
    )


if __name__ == "__main__":
    conn = get_conn()
    ensure_tables(conn)
    import_sent_invitations(conn)
    backfill_profiles_and_counters()
    enrich_limit = int(os.environ.get("OPENALEX_ENRICH_LIMIT", "500"))
    enrich_profiles_from_openalex(conn, limit=max(0, enrich_limit))
    import_retraction_watch(conn)
    conn.close()
    print("\nAll imports complete.")
