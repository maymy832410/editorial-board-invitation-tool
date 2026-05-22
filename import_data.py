"""One-time script to import sent_invitations CSV and retraction_watch.csv into Railway PostgreSQL.

Usage:
    railway run python import_data.py          # on Railway
    DATABASE_URL=... python import_data.py     # locally with env var
"""

import csv
import os
import sys
from typing import Any, Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras


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
            CREATE TABLE IF NOT EXISTS author_invitations (
                id              SERIAL PRIMARY KEY,
                orcid_id        TEXT NOT NULL,
                invitation_type TEXT NOT NULL DEFAULT 'editorial',
                author_name     TEXT DEFAULT '',
                email           TEXT DEFAULT '',
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

    rows: list[tuple[str, str, str, str, Optional[str]]] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            orcid_id = row.get("orcid_id", "").strip()
            if not orcid_id:
                continue
            rows.append((
                orcid_id,
                row.get("author_name", "").strip(),
                row.get("email", "").strip(),
                row.get("publisher", "").strip(),
                row.get("sent_at", "").strip() or None,
            ))

    if not rows:
        print("No rows found in sent_invitations CSV.")
        return

    print(f"Importing {len(rows)} sent invitations...")
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(  # type: ignore[reportUnknownMemberType]
            cur,
            """
            INSERT INTO sent_invitations (orcid_id, author_name, email, publisher, sent_at)
            VALUES %s
            ON CONFLICT (orcid_id) DO NOTHING;
            """,
            rows,
            page_size=1000,
        )
        typed_rows: list[tuple[str, str, str, str, str, str, str, str, str, Optional[str]]] = [
            (orcid_id, "editorial", author_name, email, publisher, "", "", "", "", sent_at)
            for orcid_id, author_name, email, publisher, sent_at in rows
        ]
        psycopg2.extras.execute_values(  # type: ignore[reportUnknownMemberType]
            cur,
            """
            INSERT INTO author_invitations
                (orcid_id, invitation_type, author_name, email, publisher, journal_name,
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


if __name__ == "__main__":
    conn = get_conn()
    ensure_tables(conn)
    import_sent_invitations(conn)
    import_retraction_watch(conn)
    conn.close()
    print("\nAll imports complete.")
