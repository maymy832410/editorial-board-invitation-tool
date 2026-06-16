"""PostgreSQL client for persistent storage of sent invitations (Railway)."""

import os
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Set, Dict, List, Any
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras


INVITATION_TYPE_EDITORIAL = "editorial"
INVITATION_TYPE_PUBLICATION = "publication"
OPENALEX_MATCH_STATUS_UNKNOWN = "unknown"
OPENALEX_MATCH_STATUS_MATCHED = "matched"
OPENALEX_MATCH_STATUS_PENDING_MANUAL = "pending_manual"
DEFAULT_PROFILE_COOLDOWN_DAYS = 180

# Background collection-service run states
RUN_STATUS_IDLE = "idle"
RUN_STATUS_ACTIVE = "active"
RUN_STATUS_COOLDOWN = "cooldown"
RUN_STATUS_RECOVERY = "recovery"
RUN_STATUS_STOPPED_TODAY = "stopped_today"
RUN_STATUS_PAUSED = "paused"

# Harvested-author email fetch states
EMAIL_STATUS_PENDING = "pending"
EMAIL_STATUS_FOUND = "found"
EMAIL_STATUS_NO_EMAIL = "no_email"
EMAIL_STATUS_NO_ORCID = "no_orcid"
EMAIL_STATUS_ERROR = "error"

# Background bulk email job states
BULK_JOB_STATUS_QUEUED = "queued"
BULK_JOB_STATUS_RUNNING = "running"
BULK_JOB_STATUS_COMPLETED = "completed"
BULK_JOB_STATUS_CANCELLED = "cancelled"
BULK_JOB_STATUS_FAILED = "failed"

BULK_RECIPIENT_STATUS_PENDING = "pending"
BULK_RECIPIENT_STATUS_SENDING = "sending"
BULK_RECIPIENT_STATUS_SENT = "sent"
BULK_RECIPIENT_STATUS_FAILED = "failed"
BULK_RECIPIENT_STATUS_SKIPPED = "skipped"


def _normalize_text(value: Optional[str]) -> str:
    """Normalize optional text for consistent storage."""
    return (value or "").strip()


def _normalize_email(email: Optional[str]) -> str:
    """Normalize email to lowercase and validate basic structure."""
    value = _normalize_text(email).lower()
    if "@" not in value:
        return ""
    return value


def _normalize_orcid(orcid_id: Optional[str]) -> str:
    """Normalize ORCID values to plain 16-digit form with dashes."""
    value = _normalize_text(orcid_id).lower()
    if not value:
        return ""
    value = value.replace("https://orcid.org/", "").replace("http://orcid.org/", "")
    return value


def _normalize_openalex_id(openalex_id: Optional[str]) -> str:
    """Normalize OpenAlex IDs for deterministic matching."""
    value = _normalize_text(openalex_id)
    if not value:
        return ""
    return value.rstrip("/")


def _normalize_author_name(author_name: Optional[str]) -> str:
    """Normalize author names for fallback profile identity."""
    value = _normalize_text(author_name).lower()
    if not value:
        return ""
    return re.sub(r"\s+", " ", value)


def _extract_email_domain(email: Optional[str]) -> str:
    """Extract and normalize an email domain."""
    normalized = _normalize_email(email)
    if not normalized:
        return ""
    domain = normalized.rsplit("@", 1)[-1].strip()
    return domain if "." in domain else ""


def _build_profile_key(
    orcid_id: Optional[str] = "",
    email: Optional[str] = "",
    openalex_id: Optional[str] = "",
    author_name: Optional[str] = "",
) -> str:
    """Build stable profile key from strongest available identity signal."""
    normalized_email = _normalize_email(email)
    if normalized_email:
        return f"email:{normalized_email}"

    normalized_orcid = _normalize_orcid(orcid_id)
    if normalized_orcid:
        return f"orcid:{normalized_orcid}"

    normalized_openalex = _normalize_openalex_id(openalex_id).lower()
    if normalized_openalex:
        return f"openalex:{normalized_openalex}"

    normalized_name = _normalize_author_name(author_name)
    if not normalized_name:
        return ""

    slug = re.sub(r"[^a-z0-9]+", "-", normalized_name).strip("-")
    return f"name:{slug[:120]}" if slug else ""


def _get_connection_params():
    """Get PostgreSQL connection params from DATABASE_URL (Railway provides this)."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return None

    parsed = urlparse(database_url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": parsed.path.lstrip("/"),
        "user": parsed.username,
        "password": parsed.password,
        "sslmode": "disable",
    }


class PostgresStorage:
    """Persistent storage using PostgreSQL for sent invitations."""

    TABLE_NAME = "sent_invitations"
    INVITATION_TABLE_NAME = "author_invitations"
    PROFILE_TABLE_NAME = "author_profiles"
    COLLECTION_RUNS_TABLE = "collection_runs"
    HARVESTED_AUTHORS_TABLE = "harvested_authors"
    COLLECTION_DAILY_STATS_TABLE = "collection_daily_stats"
    BULK_EMAIL_JOBS_TABLE = "bulk_email_jobs"
    BULK_EMAIL_RECIPIENTS_TABLE = "bulk_email_recipients"

    def __init__(self):
        self.available = False
        self.error_message = ""
        self._conn = None
        self._init_client()

    def _init_client(self):
        """Initialize PostgreSQL connection and ensure tables exist."""
        params = _get_connection_params()
        if not params:
            self.error_message = "DATABASE_URL not configured"
            print(self.error_message)
            return

        try:
            self._conn = psycopg2.connect(**params)
            self._conn.autocommit = True
            self._ensure_tables()
            self.available = True
            self.error_message = ""
        except Exception as e:
            self.error_message = f"PostgreSQL error: {str(e)}"
            print(self.error_message)
            self.available = False

    def _ensure_tables(self):
        """Create all required tables if they don't exist."""
        with self._conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                    orcid_id   TEXT PRIMARY KEY,
                    author_name TEXT DEFAULT '',
                    email      TEXT DEFAULT '',
                    publisher  TEXT DEFAULT '',
                    sent_at    TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute(f"""
                ALTER TABLE {self.TABLE_NAME}
                ADD COLUMN IF NOT EXISTS email_domain TEXT DEFAULT '';
            """)
            cur.execute(f"""
                ALTER TABLE {self.TABLE_NAME}
                ADD COLUMN IF NOT EXISTS openalex_id TEXT DEFAULT '';
            """)
            cur.execute(f"""
                ALTER TABLE {self.TABLE_NAME}
                ADD COLUMN IF NOT EXISTS scientific_domain TEXT DEFAULT '';
            """)
            cur.execute(f"""
                ALTER TABLE {self.TABLE_NAME}
                ADD COLUMN IF NOT EXISTS invitation_count INTEGER DEFAULT 0;
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.INVITATION_TABLE_NAME} (
                    id              SERIAL PRIMARY KEY,
                    orcid_id        TEXT NOT NULL,
                    invitation_type TEXT NOT NULL DEFAULT '{INVITATION_TYPE_EDITORIAL}',
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
            cur.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_author_invitations_unique
                ON {self.INVITATION_TABLE_NAME} (orcid_id, invitation_type, journal_name);
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_author_invitations_type
                ON {self.INVITATION_TABLE_NAME} (invitation_type);
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.PROFILE_TABLE_NAME} (
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
                    openalex_match_status        TEXT DEFAULT '{OPENALEX_MATCH_STATUS_UNKNOWN}',
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
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_author_profiles_email_lower
                ON {self.PROFILE_TABLE_NAME} (email_lower);
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_author_profiles_orcid
                ON {self.PROFILE_TABLE_NAME} (orcid_id);
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_author_profiles_openalex
                ON {self.PROFILE_TABLE_NAME} (openalex_id);
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_author_profiles_domain
                ON {self.PROFILE_TABLE_NAME} (email_domain);
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_author_profiles_match_status
                ON {self.PROFILE_TABLE_NAME} (openalex_match_status);
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_author_profiles_cooldown
                ON {self.PROFILE_TABLE_NAME} (cooldown_until);
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
            self._ensure_collection_tables(cur)
            self._ensure_bulk_email_tables(cur)

    def _ensure_collection_tables(self, cur):
        """Create tables for the background email-collection service."""
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.COLLECTION_RUNS_TABLE} (
                id                      INTEGER PRIMARY KEY,
                status                  TEXT DEFAULT '{RUN_STATUS_IDLE}',
                domains_json            TEXT DEFAULT '[]',
                disciplines_json        TEXT DEFAULT '[]',
                specialties_json        TEXT DEFAULT '[]',
                exclude_countries_json  TEXT DEFAULT '[]',
                keyword_tags            TEXT DEFAULT '',
                topic_ids_json          TEXT DEFAULT '[]',
                h_index_min             INTEGER,
                h_index_max             INTEGER,
                baseline_concurrency    INTEGER DEFAULT 2,
                baseline_delay          DOUBLE PRECISION DEFAULT 3.0,
                effective_concurrency   INTEGER DEFAULT 2,
                effective_delay         DOUBLE PRECISION DEFAULT 3.0,
                seed_cursor             TEXT DEFAULT '*',
                seed_exhausted          BOOLEAN DEFAULT FALSE,
                last_429_at             TIMESTAMPTZ,
                cooldown_until          TIMESTAMPTZ,
                stop_until              TIMESTAMPTZ,
                run_429_count           INTEGER DEFAULT 0,
                clean_batches           INTEGER DEFAULT 0,
                created_at              TIMESTAMPTZ DEFAULT NOW(),
                updated_at              TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.HARVESTED_AUTHORS_TABLE} (
                openalex_id     TEXT PRIMARY KEY,
                orcid_id        TEXT DEFAULT '',
                author_name     TEXT DEFAULT '',
                author_name_lower TEXT DEFAULT '',
                h_index         INTEGER,
                works_count     INTEGER,
                cited_by_count  INTEGER,
                institution     TEXT DEFAULT '',
                country         TEXT DEFAULT '',
                discipline      TEXT DEFAULT '',
                specialty       TEXT DEFAULT '',
                subfield        TEXT DEFAULT '',
                research_areas  TEXT DEFAULT '',
                all_topics_json TEXT DEFAULT '[]',
                email           TEXT DEFAULT '',
                all_emails      TEXT DEFAULT '',
                email_source    TEXT DEFAULT '',
                email_status    TEXT DEFAULT '{EMAIL_STATUS_PENDING}',
                attempts        INTEGER DEFAULT 0,
                last_checked_at TIMESTAMPTZ,
                next_retry_at   TIMESTAMPTZ,
                run_id          INTEGER,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                updated_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_harvested_email_status
            ON {self.HARVESTED_AUTHORS_TABLE} (email_status);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_harvested_orcid
            ON {self.HARVESTED_AUTHORS_TABLE} (orcid_id);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_harvested_discipline
            ON {self.HARVESTED_AUTHORS_TABLE} (discipline);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_harvested_country
            ON {self.HARVESTED_AUTHORS_TABLE} (country);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_harvested_next_retry
            ON {self.HARVESTED_AUTHORS_TABLE} (next_retry_at);
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.COLLECTION_DAILY_STATS_TABLE} (
                day             DATE PRIMARY KEY,
                emails_found    INTEGER DEFAULT 0,
                attempts        INTEGER DEFAULT 0,
                orcid_429       INTEGER DEFAULT 0,
                openalex_429    INTEGER DEFAULT 0,
                seeded          INTEGER DEFAULT 0
            );
        """)

    def _ensure_bulk_email_tables(self, cur):
        """Create tables for durable background bulk email sends."""
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.BULK_EMAIL_JOBS_TABLE} (
                id                  SERIAL PRIMARY KEY,
                status              TEXT DEFAULT '{BULK_JOB_STATUS_QUEUED}',
                invitation_type     TEXT NOT NULL DEFAULT '{INVITATION_TYPE_EDITORIAL}',
                publisher_id        TEXT DEFAULT '',
                journal_name        TEXT DEFAULT '',
                template_id         TEXT DEFAULT '',
                template_strategy   TEXT DEFAULT '',
                scopus_indexed      BOOLEAN DEFAULT FALSE,
                attach_pdf          BOOLEAN DEFAULT TRUE,
                include_publications BOOLEAN DEFAULT FALSE,
                journal_config_json TEXT DEFAULT '{{}}',
                total_count         INTEGER DEFAULT 0,
                pending_count       INTEGER DEFAULT 0,
                sent_count          INTEGER DEFAULT 0,
                failed_count        INTEGER DEFAULT 0,
                skipped_count       INTEGER DEFAULT 0,
                last_recipient      TEXT DEFAULT '',
                last_error          TEXT DEFAULT '',
                last_provider_response TEXT DEFAULT '',
                cancel_requested    BOOLEAN DEFAULT FALSE,
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                started_at          TIMESTAMPTZ,
                completed_at        TIMESTAMPTZ,
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.BULK_EMAIL_RECIPIENTS_TABLE} (
                id                       SERIAL PRIMARY KEY,
                job_id                   INTEGER NOT NULL REFERENCES {self.BULK_EMAIL_JOBS_TABLE}(id) ON DELETE CASCADE,
                status                   TEXT DEFAULT '{BULK_RECIPIENT_STATUS_PENDING}',
                orcid_id                 TEXT DEFAULT '',
                author_name              TEXT DEFAULT '',
                email                    TEXT DEFAULT '',
                openalex_id              TEXT DEFAULT '',
                specialty                TEXT DEFAULT '',
                research_areas           TEXT DEFAULT '',
                all_topics_json          TEXT DEFAULT '[]',
                recent_publications_json TEXT DEFAULT '[]',
                template_id              TEXT DEFAULT '',
                attempts                 INTEGER DEFAULT 0,
                last_error               TEXT DEFAULT '',
                provider_response        TEXT DEFAULT '',
                claimed_at               TIMESTAMPTZ,
                sent_at                  TIMESTAMPTZ,
                created_at               TIMESTAMPTZ DEFAULT NOW(),
                updated_at               TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_bulk_jobs_status
            ON {self.BULK_EMAIL_JOBS_TABLE} (status, created_at);
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_bulk_recipients_job_status
            ON {self.BULK_EMAIL_RECIPIENTS_TABLE} (job_id, status, id);
        """)
        cur.execute(f"""
            ALTER TABLE {self.BULK_EMAIL_JOBS_TABLE}
            ADD COLUMN IF NOT EXISTS last_provider_response TEXT DEFAULT '';
        """)
        cur.execute(f"""
            ALTER TABLE {self.BULK_EMAIL_RECIPIENTS_TABLE}
            ADD COLUMN IF NOT EXISTS provider_response TEXT DEFAULT '';
        """)

    def _get_cursor(self):
        """Get a cursor, reconnecting if the connection was lost."""
        try:
            self._conn.cursor().close()  # quick liveness check
        except Exception:
            params = _get_connection_params()
            if params:
                self._conn = psycopg2.connect(**params)
                self._conn.autocommit = True
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def _upsert_author_profile(
        self,
        orcid_id: str = "",
        author_name: str = "",
        email: str = "",
        publisher: str = "",
        openalex_id: str = "",
        scientific_domain: str = "",
        scientific_domains_json: str = "[]",
        match_status: str = OPENALEX_MATCH_STATUS_UNKNOWN,
        match_confidence: float = 0,
        source: str = "",
    ) -> str:
        """Upsert author profile row and return the resolved profile key."""
        if not self.available:
            return ""

        normalized_orcid = _normalize_orcid(orcid_id)
        normalized_email = _normalize_email(email)
        normalized_domain = _extract_email_domain(email)
        normalized_openalex = _normalize_openalex_id(openalex_id)
        normalized_name = _normalize_text(author_name)
        normalized_name_lower = _normalize_author_name(author_name)

        profile_key = _build_profile_key(
            orcid_id=normalized_orcid,
            email=normalized_email,
            openalex_id=normalized_openalex,
            author_name=normalized_name,
        )
        if not profile_key:
            return ""

        serialized_domains = scientific_domains_json or "[]"
        if not isinstance(serialized_domains, str):
            try:
                serialized_domains = json.dumps(serialized_domains)
            except Exception:
                serialized_domains = "[]"

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.PROFILE_TABLE_NAME}
                        (profile_key, orcid_id, openalex_id, author_name, author_name_lower,
                         email, email_lower, email_domain, scientific_domain,
                         scientific_domains_json, openalex_match_status, openalex_match_confidence,
                         publisher, source, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (profile_key) DO UPDATE SET
                        orcid_id = CASE
                            WHEN EXCLUDED.orcid_id <> '' THEN EXCLUDED.orcid_id
                            ELSE {self.PROFILE_TABLE_NAME}.orcid_id
                        END,
                        openalex_id = CASE
                            WHEN EXCLUDED.openalex_id <> '' THEN EXCLUDED.openalex_id
                            ELSE {self.PROFILE_TABLE_NAME}.openalex_id
                        END,
                        author_name = CASE
                            WHEN EXCLUDED.author_name <> '' THEN EXCLUDED.author_name
                            ELSE {self.PROFILE_TABLE_NAME}.author_name
                        END,
                        author_name_lower = CASE
                            WHEN EXCLUDED.author_name_lower <> '' THEN EXCLUDED.author_name_lower
                            ELSE {self.PROFILE_TABLE_NAME}.author_name_lower
                        END,
                        email = CASE
                            WHEN EXCLUDED.email <> '' THEN EXCLUDED.email
                            ELSE {self.PROFILE_TABLE_NAME}.email
                        END,
                        email_lower = CASE
                            WHEN EXCLUDED.email_lower <> '' THEN EXCLUDED.email_lower
                            ELSE {self.PROFILE_TABLE_NAME}.email_lower
                        END,
                        email_domain = CASE
                            WHEN EXCLUDED.email_domain <> '' THEN EXCLUDED.email_domain
                            ELSE {self.PROFILE_TABLE_NAME}.email_domain
                        END,
                        scientific_domain = CASE
                            WHEN EXCLUDED.scientific_domain <> '' THEN EXCLUDED.scientific_domain
                            ELSE {self.PROFILE_TABLE_NAME}.scientific_domain
                        END,
                        scientific_domains_json = CASE
                            WHEN EXCLUDED.scientific_domains_json <> '[]' THEN EXCLUDED.scientific_domains_json
                            ELSE {self.PROFILE_TABLE_NAME}.scientific_domains_json
                        END,
                        openalex_match_status = CASE
                            WHEN EXCLUDED.openalex_match_status <> %s
                                THEN EXCLUDED.openalex_match_status
                            ELSE {self.PROFILE_TABLE_NAME}.openalex_match_status
                        END,
                        openalex_match_confidence = GREATEST(
                            {self.PROFILE_TABLE_NAME}.openalex_match_confidence,
                            EXCLUDED.openalex_match_confidence
                        ),
                        publisher = CASE
                            WHEN EXCLUDED.publisher <> '' THEN EXCLUDED.publisher
                            ELSE {self.PROFILE_TABLE_NAME}.publisher
                        END,
                        source = CASE
                            WHEN EXCLUDED.source <> '' THEN EXCLUDED.source
                            ELSE {self.PROFILE_TABLE_NAME}.source
                        END,
                        updated_at = NOW();
                    """,
                    (
                        profile_key,
                        normalized_orcid,
                        normalized_openalex,
                        normalized_name,
                        normalized_name_lower,
                        normalized_email,
                        normalized_email,
                        normalized_domain,
                        _normalize_text(scientific_domain),
                        serialized_domains,
                        match_status or OPENALEX_MATCH_STATUS_UNKNOWN,
                        float(match_confidence or 0),
                        _normalize_text(publisher),
                        _normalize_text(source),
                        OPENALEX_MATCH_STATUS_UNKNOWN,
                    ),
                )
            return profile_key
        except Exception as e:
            print(f"PostgreSQL upsert profile error: {e}")
            return ""

    def _increment_profile_invite_counter(
        self,
        profile_key: str,
        invitation_type: str,
        sent_at: datetime,
        cooldown_days: int = DEFAULT_PROFILE_COOLDOWN_DAYS,
    ) -> bool:
        """Increment profile invitation counters after each send event."""
        if not self.available or not profile_key:
            return False

        editorial_inc = 1 if invitation_type == INVITATION_TYPE_EDITORIAL else 0
        publication_inc = 1 if invitation_type == INVITATION_TYPE_PUBLICATION else 0
        cooldown_until = sent_at + timedelta(days=max(0, int(cooldown_days or 0)))

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.PROFILE_TABLE_NAME}
                    SET invitation_count_total = invitation_count_total + 1,
                        invitation_count_editorial = invitation_count_editorial + %s,
                        invitation_count_publication = invitation_count_publication + %s,
                        last_invited_at = %s,
                        cooldown_until = %s,
                        updated_at = NOW()
                    WHERE profile_key = %s;
                    """,
                    (editorial_inc, publication_inc, sent_at, cooldown_until, profile_key),
                )
            return True
        except Exception as e:
            print(f"PostgreSQL increment profile counter error: {e}")
            return False

    def _set_profile_invitation_snapshot(
        self,
        profile_key: str,
        total_count: int,
        editorial_count: int,
        publication_count: int,
        last_invited_at: Optional[datetime],
        cooldown_days: int = DEFAULT_PROFILE_COOLDOWN_DAYS,
    ) -> bool:
        """Set invitation counters using max semantics for idempotent backfills."""
        if not self.available or not profile_key:
            return False

        cooldown_until = None
        if last_invited_at is not None:
            cooldown_until = last_invited_at + timedelta(days=max(0, int(cooldown_days or 0)))

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.PROFILE_TABLE_NAME}
                    SET invitation_count_total = GREATEST(invitation_count_total, %s),
                        invitation_count_editorial = GREATEST(invitation_count_editorial, %s),
                        invitation_count_publication = GREATEST(invitation_count_publication, %s),
                        last_invited_at = CASE
                            WHEN %s IS NULL THEN last_invited_at
                            WHEN last_invited_at IS NULL OR last_invited_at < %s THEN %s
                            ELSE last_invited_at
                        END,
                        cooldown_until = CASE
                            WHEN %s IS NULL THEN cooldown_until
                            WHEN cooldown_until IS NULL OR cooldown_until < %s THEN %s
                            ELSE cooldown_until
                        END,
                        updated_at = NOW()
                    WHERE profile_key = %s;
                    """,
                    (
                        int(total_count or 0),
                        int(editorial_count or 0),
                        int(publication_count or 0),
                        last_invited_at,
                        last_invited_at,
                        last_invited_at,
                        cooldown_until,
                        cooldown_until,
                        cooldown_until,
                        profile_key,
                    ),
                )
            return True
        except Exception as e:
            print(f"PostgreSQL set profile snapshot error: {e}")
            return False

    def _mark_invitation_record(
        self,
        orcid_id: str,
        author_name: str = "",
        email: str = "",
        publisher: str = "",
        invitation_type: str = INVITATION_TYPE_EDITORIAL,
        journal_name: str = "",
        template_id: str = "",
        cite_score: str = "",
        quartile: str = "",
        sent_at: Optional[datetime] = None,
    ) -> bool:
        """Upsert the invitation-type-aware send log."""
        if not self.available or not orcid_id:
            return False
        effective_sent_at = sent_at or datetime.now(timezone.utc)
        try:
            with self._get_cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {self.INVITATION_TABLE_NAME}
                        (orcid_id, invitation_type, author_name, email, email_domain, publisher, journal_name,
                         template_id, cite_score, quartile, sent_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (orcid_id, invitation_type, journal_name) DO UPDATE SET
                        author_name = EXCLUDED.author_name,
                        email       = EXCLUDED.email,
                        email_domain = EXCLUDED.email_domain,
                        publisher   = EXCLUDED.publisher,
                        template_id = EXCLUDED.template_id,
                        cite_score  = EXCLUDED.cite_score,
                        quartile    = EXCLUDED.quartile,
                        sent_at     = EXCLUDED.sent_at;
                """, (
                    orcid_id,
                    invitation_type,
                    author_name,
                    email,
                    _extract_email_domain(email),
                    publisher,
                    journal_name,
                    template_id,
                    cite_score,
                    quartile,
                    effective_sent_at,
                ))
            return True
        except Exception as e:
            print(f"PostgreSQL mark invitation record error: {e}")
            return False

    def get_status(self) -> Dict:
        """Get database status for UI display."""
        return {
            "available": self.available,
            "error": self.error_message,
            "table": self.TABLE_NAME,
            "profile_table": self.PROFILE_TABLE_NAME,
        }

    def upsert_author_profile(
        self,
        orcid_id: str = "",
        author_name: str = "",
        email: str = "",
        publisher: str = "",
        openalex_id: str = "",
        scientific_domain: str = "",
        scientific_domains_json: str = "[]",
        match_status: str = OPENALEX_MATCH_STATUS_UNKNOWN,
        match_confidence: float = 0,
        source: str = "",
    ) -> bool:
        """Public helper to upsert a profile row."""
        profile_key = self._upsert_author_profile(
            orcid_id=orcid_id,
            author_name=author_name,
            email=email,
            publisher=publisher,
            openalex_id=openalex_id,
            scientific_domain=scientific_domain,
            scientific_domains_json=scientific_domains_json,
            match_status=match_status,
            match_confidence=match_confidence,
            source=source,
        )
        return bool(profile_key)

    def backfill_author_profiles(self, max_rows: Optional[int] = None) -> Dict[str, int]:
        """Backfill profile records and invitation counters from historical tables."""
        stats = {
            "seeded_profiles": 0,
            "counter_snapshots": 0,
            "pending_manual_marked": 0,
        }
        if not self.available:
            return stats

        limit_clause = ""
        params: tuple[Any, ...] = ()
        if max_rows is not None and int(max_rows) > 0:
            limit_clause = " LIMIT %s"
            params = (int(max_rows),)

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT orcid_id, author_name, email, publisher, sent_at
                    FROM {self.TABLE_NAME}
                    ORDER BY sent_at DESC{limit_clause};
                    """,
                    params,
                )
                sent_rows = [dict(row) for row in cur.fetchall()]

            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT orcid_id, invitation_type, author_name, email, publisher, sent_at
                    FROM {self.INVITATION_TABLE_NAME}
                    ORDER BY sent_at DESC{limit_clause};
                    """,
                    params,
                )
                invitation_rows = [dict(row) for row in cur.fetchall()]

            profile_seed: Dict[str, Dict[str, Any]] = {}
            for row in sent_rows + invitation_rows:
                profile_key = _build_profile_key(
                    orcid_id=row.get("orcid_id", ""),
                    email=row.get("email", ""),
                    author_name=row.get("author_name", ""),
                )
                if not profile_key:
                    continue

                profile = profile_seed.setdefault(
                    profile_key,
                    {
                        "orcid_id": "",
                        "author_name": "",
                        "email": "",
                        "publisher": "",
                    },
                )
                if row.get("orcid_id") and not profile["orcid_id"]:
                    profile["orcid_id"] = row["orcid_id"]
                if row.get("author_name") and not profile["author_name"]:
                    profile["author_name"] = row["author_name"]
                if row.get("email") and not profile["email"]:
                    profile["email"] = row["email"]
                if row.get("publisher") and not profile["publisher"]:
                    profile["publisher"] = row["publisher"]

            for profile in profile_seed.values():
                if self._upsert_author_profile(
                    orcid_id=profile["orcid_id"],
                    author_name=profile["author_name"],
                    email=profile["email"],
                    publisher=profile["publisher"],
                    source="backfill_history",
                ):
                    stats["seeded_profiles"] += 1

            aggregate_counts: Dict[str, Dict[str, Any]] = {}
            for row in invitation_rows:
                profile_key = _build_profile_key(
                    orcid_id=row.get("orcid_id", ""),
                    email=row.get("email", ""),
                    author_name=row.get("author_name", ""),
                )
                if not profile_key:
                    continue
                aggregate = aggregate_counts.setdefault(
                    profile_key,
                    {
                        "total": 0,
                        "editorial": 0,
                        "publication": 0,
                        "last_invited_at": None,
                    },
                )
                aggregate["total"] += 1
                if row.get("invitation_type") == INVITATION_TYPE_PUBLICATION:
                    aggregate["publication"] += 1
                else:
                    aggregate["editorial"] += 1

                sent_at = row.get("sent_at")
                if sent_at and (
                    aggregate["last_invited_at"] is None
                    or sent_at > aggregate["last_invited_at"]
                ):
                    aggregate["last_invited_at"] = sent_at

            for profile_key, aggregate in aggregate_counts.items():
                if self._set_profile_invitation_snapshot(
                    profile_key=profile_key,
                    total_count=aggregate["total"],
                    editorial_count=aggregate["editorial"],
                    publication_count=aggregate["publication"],
                    last_invited_at=aggregate["last_invited_at"],
                ):
                    stats["counter_snapshots"] += 1

            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.PROFILE_TABLE_NAME}
                    SET openalex_match_status = %s,
                        updated_at = NOW()
                    WHERE (orcid_id = '' OR orcid_id IS NULL)
                      AND (openalex_id = '' OR openalex_id IS NULL)
                      AND openalex_match_status = %s;
                    """,
                    (OPENALEX_MATCH_STATUS_PENDING_MANUAL, OPENALEX_MATCH_STATUS_UNKNOWN),
                )
                stats["pending_manual_marked"] = cur.rowcount or 0

            return stats
        except Exception as e:
            print(f"PostgreSQL backfill author profiles error: {e}")
            return stats

    def get_profiles_needing_openalex(
        self,
        limit: int = 500,
        require_orcid: bool = True,
        include_pending_manual: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return profiles missing OpenAlex IDs that still need enrichment work."""
        if not self.available:
            return []
        try:
            with self._get_cursor() as cur:
                status_filter = [OPENALEX_MATCH_STATUS_UNKNOWN]
                if include_pending_manual:
                    status_filter.append(OPENALEX_MATCH_STATUS_PENDING_MANUAL)

                if require_orcid:
                    cur.execute(
                        f"""
                        SELECT profile_key, orcid_id, author_name, email, email_domain
                        FROM {self.PROFILE_TABLE_NAME}
                        WHERE (openalex_id = '' OR openalex_id IS NULL)
                          AND (orcid_id <> '' AND orcid_id IS NOT NULL)
                          AND COALESCE(openalex_match_status, %s) = ANY(%s)
                        ORDER BY updated_at ASC NULLS FIRST
                        LIMIT %s;
                        """,
                        (
                            OPENALEX_MATCH_STATUS_UNKNOWN,
                            status_filter,
                            int(limit),
                        ),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT profile_key, orcid_id, author_name, email, email_domain
                        FROM {self.PROFILE_TABLE_NAME}
                        WHERE (openalex_id = '' OR openalex_id IS NULL)
                          AND COALESCE(openalex_match_status, %s) = ANY(%s)
                        ORDER BY updated_at ASC NULLS FIRST
                        LIMIT %s;
                        """,
                        (
                            OPENALEX_MATCH_STATUS_UNKNOWN,
                            status_filter,
                            int(limit),
                        ),
                    )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"PostgreSQL get profiles needing OpenAlex error: {e}")
            return []

    def count_profiles_needing_openalex(
        self,
        require_orcid: bool = True,
        include_pending_manual: bool = False,
    ) -> int:
        """Return how many profiles remain in the OpenAlex enrichment queue."""
        if not self.available:
            return 0

        status_filter = [OPENALEX_MATCH_STATUS_UNKNOWN]
        if include_pending_manual:
            status_filter.append(OPENALEX_MATCH_STATUS_PENDING_MANUAL)

        try:
            with self._get_cursor() as cur:
                if require_orcid:
                    cur.execute(
                        f"""
                        SELECT COUNT(*) AS cnt
                        FROM {self.PROFILE_TABLE_NAME}
                        WHERE (openalex_id = '' OR openalex_id IS NULL)
                          AND (orcid_id <> '' AND orcid_id IS NOT NULL)
                          AND COALESCE(openalex_match_status, %s) = ANY(%s);
                        """,
                        (
                            OPENALEX_MATCH_STATUS_UNKNOWN,
                            status_filter,
                        ),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT COUNT(*) AS cnt
                        FROM {self.PROFILE_TABLE_NAME}
                        WHERE (openalex_id = '' OR openalex_id IS NULL)
                          AND COALESCE(openalex_match_status, %s) = ANY(%s);
                        """,
                        (
                            OPENALEX_MATCH_STATUS_UNKNOWN,
                            status_filter,
                        ),
                    )

                row = cur.fetchone() or {}
                return int(row.get("cnt") or 0)
        except Exception as e:
            print(f"PostgreSQL count profiles needing OpenAlex error: {e}")
            return 0

    def update_profile_openalex(
        self,
        profile_key: str,
        openalex_id: str,
        scientific_domain: str = "",
        scientific_domains: Optional[List[str]] = None,
        match_status: str = OPENALEX_MATCH_STATUS_MATCHED,
        match_confidence: float = 1.0,
    ) -> bool:
        """Save OpenAlex enrichment fields for a specific profile."""
        if not self.available or not profile_key:
            return False

        serialized_domains = "[]"
        if scientific_domains:
            serialized_domains = json.dumps(scientific_domains)

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.PROFILE_TABLE_NAME}
                    SET openalex_id = %s,
                        scientific_domain = %s,
                        scientific_domains_json = %s,
                        openalex_match_status = %s,
                        openalex_match_confidence = %s,
                        updated_at = NOW()
                    WHERE profile_key = %s;
                    """,
                    (
                        _normalize_openalex_id(openalex_id),
                        _normalize_text(scientific_domain),
                        serialized_domains,
                        match_status or OPENALEX_MATCH_STATUS_MATCHED,
                        float(match_confidence or 0),
                        profile_key,
                    ),
                )
                return (cur.rowcount or 0) > 0
        except Exception as e:
            print(f"PostgreSQL update profile OpenAlex error: {e}")
            return False

    def get_profile_summary(self) -> Dict[str, int]:
        """Get aggregate counts for profile enrichment monitoring."""
        summary = {
            "total_profiles": 0,
            "matched_openalex": 0,
            "pending_manual": 0,
            "cooldown_active": 0,
        }
        if not self.available:
            return summary
        try:
            with self._get_cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {self.PROFILE_TABLE_NAME};")
                summary["total_profiles"] = int((cur.fetchone() or {}).get("cnt", 0))

                cur.execute(
                    f"""
                    SELECT COUNT(*) AS cnt FROM {self.PROFILE_TABLE_NAME}
                    WHERE openalex_match_status = %s;
                    """,
                    (OPENALEX_MATCH_STATUS_MATCHED,),
                )
                summary["matched_openalex"] = int((cur.fetchone() or {}).get("cnt", 0))

                cur.execute(
                    f"""
                    SELECT COUNT(*) AS cnt FROM {self.PROFILE_TABLE_NAME}
                    WHERE openalex_match_status = %s;
                    """,
                    (OPENALEX_MATCH_STATUS_PENDING_MANUAL,),
                )
                summary["pending_manual"] = int((cur.fetchone() or {}).get("cnt", 0))

                cur.execute(
                    f"""
                    SELECT COUNT(*) AS cnt FROM {self.PROFILE_TABLE_NAME}
                    WHERE cooldown_until IS NOT NULL AND cooldown_until > NOW();
                    """,
                )
                summary["cooldown_active"] = int((cur.fetchone() or {}).get("cnt", 0))
            return summary
        except Exception as e:
            print(f"PostgreSQL profile summary error: {e}")
            return summary

    def get_author_profile_candidates(
        self,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return invitation candidates from author_profiles requiring ORCID and email."""
        if not self.available:
            return []

        safe_limit = max(1, min(int(limit or 500), 5000))
        safe_offset = max(0, int(offset or 0))

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        profile_key,
                        orcid_id,
                        openalex_id,
                        author_name,
                        email,
                        scientific_domain,
                        scientific_domains_json,
                        invitation_count_total,
                        invitation_count_editorial,
                        invitation_count_publication,
                        last_invited_at,
                        updated_at
                    FROM {self.PROFILE_TABLE_NAME}
                    WHERE (orcid_id <> '' AND orcid_id IS NOT NULL)
                      AND (email <> '' AND email IS NOT NULL)
                    ORDER BY updated_at DESC
                    LIMIT %s OFFSET %s;
                    """,
                    (safe_limit, safe_offset),
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"PostgreSQL get author profile candidates error: {e}")
            return []

    def count_author_profile_candidates(self) -> int:
        """Return total invitation candidates from author_profiles requiring ORCID and email."""
        if not self.available:
            return 0

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM {self.PROFILE_TABLE_NAME}
                    WHERE (orcid_id <> '' AND orcid_id IS NOT NULL)
                      AND (email <> '' AND email IS NOT NULL);
                    """,
                )
                row = cur.fetchone() or {}
                return int(row.get("cnt") or 0)
        except Exception as e:
            print(f"PostgreSQL count author profile candidates error: {e}")
            return 0

    def get_invitation_counts(self, orcid_ids: List[str]) -> Dict[str, int]:
        """Get invitation counts for a list of ORCID IDs."""
        if not self.available:
            return {}

        cleaned_ids = sorted({(orcid_id or "").strip() for orcid_id in orcid_ids if orcid_id})
        if not cleaned_ids:
            return {}

        counts: Dict[str, int] = {}
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT orcid_id, invitation_count_total
                    FROM {self.PROFILE_TABLE_NAME}
                    WHERE orcid_id = ANY(%s);
                    """,
                    (cleaned_ids,),
                )
                for row in cur.fetchall():
                    orcid_id = row.get("orcid_id")
                    if not orcid_id:
                        continue
                    counts[orcid_id] = int(row.get("invitation_count_total") or 0)

            missing_ids = [orcid_id for orcid_id in cleaned_ids if orcid_id not in counts]
            if missing_ids:
                with self._get_cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT orcid_id, invitation_count
                        FROM {self.TABLE_NAME}
                        WHERE orcid_id = ANY(%s);
                        """,
                        (missing_ids,),
                    )
                    for row in cur.fetchall():
                        orcid_id = row.get("orcid_id")
                        if not orcid_id:
                            continue
                        counts[orcid_id] = int(row.get("invitation_count") or 0)
            return counts
        except Exception as e:
            print(f"PostgreSQL get invitation counts error: {e}")
            return {}

    def get_invitation_count(self, orcid_id: str) -> int:
        """Get invitation count for one ORCID ID."""
        counts = self.get_invitation_counts([orcid_id])
        return counts.get(orcid_id, 0)

    def mark_sent(
        self,
        orcid_id: str,
        author_name: str = "",
        email: str = "",
        publisher: str = "",
        invitation_type: str = INVITATION_TYPE_EDITORIAL,
        journal_name: str = "",
        template_id: str = "",
        cite_score: str = "",
        quartile: str = "",
    ) -> bool:
        """Mark an author as sent an invitation, with invitation-type tracking."""
        if not self.available:
            return False

        sent_at = datetime.now(timezone.utc)

        profile_key = self._upsert_author_profile(
            orcid_id=orcid_id,
            author_name=author_name,
            email=email,
            publisher=publisher,
            source="send_event",
        )

        typed_ok = self._mark_invitation_record(
            orcid_id=orcid_id,
            author_name=author_name,
            email=email,
            publisher=publisher,
            invitation_type=invitation_type,
            journal_name=journal_name,
            template_id=template_id,
            cite_score=cite_score,
            quartile=quartile,
            sent_at=sent_at,
        )

        if typed_ok and profile_key:
            self._increment_profile_invite_counter(
                profile_key=profile_key,
                invitation_type=invitation_type,
                sent_at=sent_at,
            )

        if invitation_type != INVITATION_TYPE_EDITORIAL:
            return typed_ok

        try:
            with self._get_cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {self.TABLE_NAME}
                        (orcid_id, author_name, email, email_domain, publisher, sent_at, invitation_count)
                    VALUES (%s, %s, %s, %s, %s, %s, 1)
                    ON CONFLICT (orcid_id) DO UPDATE SET
                        author_name = EXCLUDED.author_name,
                        email       = EXCLUDED.email,
                        email_domain = EXCLUDED.email_domain,
                        publisher   = EXCLUDED.publisher,
                        sent_at     = EXCLUDED.sent_at,
                        invitation_count = COALESCE({self.TABLE_NAME}.invitation_count, 0) + 1;
                """, (
                    orcid_id,
                    author_name,
                    email,
                    _extract_email_domain(email),
                    publisher,
                    sent_at,
                ))
            return typed_ok
        except Exception as e:
            print(f"PostgreSQL mark_sent error: {e}")
            return typed_ok

    def is_sent(
        self,
        orcid_id: str,
        invitation_type: str = INVITATION_TYPE_EDITORIAL,
        journal_name: Optional[str] = None,
    ) -> bool:
        """Check if author has been sent an invitation of the requested type."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                if journal_name is None:
                    cur.execute(
                        f"""
                        SELECT 1 FROM {self.INVITATION_TABLE_NAME}
                        WHERE orcid_id = %s AND invitation_type = %s
                        LIMIT 1;
                        """,
                        (orcid_id, invitation_type),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT 1 FROM {self.INVITATION_TABLE_NAME}
                        WHERE orcid_id = %s AND invitation_type = %s AND journal_name = %s
                        LIMIT 1;
                        """,
                        (orcid_id, invitation_type, journal_name),
                    )
                if cur.fetchone() is not None:
                    return True

                if invitation_type == INVITATION_TYPE_EDITORIAL:
                    cur.execute(
                        f"SELECT 1 FROM {self.TABLE_NAME} WHERE orcid_id = %s LIMIT 1;",
                        (orcid_id,),
                    )
                    return cur.fetchone() is not None

                return False
        except Exception as e:
            print(f"PostgreSQL is_sent error: {e}")
            return False

    def get_all_sent(
        self,
        invitation_type: str = INVITATION_TYPE_EDITORIAL,
        journal_name: Optional[str] = None,
    ) -> Set[str]:
        """Get all ORCID IDs sent an invitation of the requested type."""
        if not self.available:
            return set()
        try:
            with self._get_cursor() as cur:
                if journal_name is None:
                    cur.execute(
                        f"""
                        SELECT orcid_id FROM {self.INVITATION_TABLE_NAME}
                        WHERE invitation_type = %s;
                        """,
                        (invitation_type,),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT orcid_id FROM {self.INVITATION_TABLE_NAME}
                        WHERE invitation_type = %s AND journal_name = %s;
                        """,
                        (invitation_type, journal_name),
                    )
                sent = {row["orcid_id"] for row in cur.fetchall()}

                if invitation_type == INVITATION_TYPE_EDITORIAL:
                    cur.execute(f"SELECT orcid_id FROM {self.TABLE_NAME};")
                    sent |= {row["orcid_id"] for row in cur.fetchall()}

                return sent
        except Exception as e:
            print(f"PostgreSQL get_all_sent error: {e}")
            return set()

    def get_sent_details(self, orcid_id: str) -> Optional[Dict]:
        """Get details of a sent invitation."""
        if not self.available:
            return None
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {self.TABLE_NAME} WHERE orcid_id = %s LIMIT 1;",
                    (orcid_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"PostgreSQL get_sent_details error: {e}")
            return None

    def get_sent_count(self) -> int:
        """Get total count of sent invitations."""
        if not self.available:
            return 0
        try:
            with self._get_cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {self.TABLE_NAME};")
                row = cur.fetchone()
                return row["cnt"] if row else 0
        except Exception as e:
            print(f"PostgreSQL get_sent_count error: {e}")
            return 0

    def remove_sent(self, orcid_id: str) -> bool:
        """Remove sent status (useful for corrections)."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self.TABLE_NAME} WHERE orcid_id = %s;",
                    (orcid_id,),
                )
            return True
        except Exception as e:
            print(f"PostgreSQL remove_sent error: {e}")
            return False

    # ------------------------------------------------------------------
    # Background bulk email jobs
    # ------------------------------------------------------------------
    def create_bulk_email_job(
        self,
        recipients: List[Dict[str, Any]],
        invitation_type: str,
        publisher_id: str,
        journal_name: str = "",
        template_id: str = "",
        template_strategy: str = "",
        scopus_indexed: bool = False,
        attach_pdf: bool = True,
        include_publications: bool = False,
        journal_config: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Create a durable bulk email job and recipient queue."""
        if not self.available or not recipients:
            return None

        cleaned: List[Dict[str, Any]] = []
        seen_keys: Set[str] = set()
        for recipient in recipients:
            email = _normalize_email(recipient.get("email"))
            if not email:
                continue
            orcid_id = _normalize_orcid(recipient.get("orcid_id"))
            identity_key = orcid_id or f"email:{email}"
            if identity_key in seen_keys:
                continue
            seen_keys.add(identity_key)
            cleaned.append({**recipient, "email": email, "orcid_id": orcid_id})

        if not cleaned:
            return None

        journal_config_json = json.dumps(journal_config or {}, default=str)
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.BULK_EMAIL_JOBS_TABLE}
                        (status, invitation_type, publisher_id, journal_name, template_id,
                         template_strategy, scopus_indexed, attach_pdf, include_publications,
                         journal_config_json, total_count, pending_count, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id;
                    """,
                    (
                        BULK_JOB_STATUS_QUEUED,
                        invitation_type,
                        publisher_id,
                        journal_name if invitation_type == INVITATION_TYPE_PUBLICATION else "",
                        template_id,
                        template_strategy,
                        bool(scopus_indexed),
                        bool(attach_pdf),
                        bool(include_publications),
                        journal_config_json,
                        len(cleaned),
                        len(cleaned),
                    ),
                )
                row = cur.fetchone()
                if not row:
                    return None
                job_id = int(row["id"])

                values = []
                for recipient in cleaned:
                    all_topics = recipient.get("all_topics") or []
                    recent_publications = recipient.get("recent_publications") or []
                    values.append(
                        (
                            job_id,
                            BULK_RECIPIENT_STATUS_PENDING,
                            recipient.get("orcid_id", ""),
                            _normalize_text(recipient.get("name") or recipient.get("author_name")),
                            recipient.get("email", ""),
                            _normalize_openalex_id(recipient.get("author_id") or recipient.get("openalex_id")),
                            _normalize_text(recipient.get("specialty")),
                            _normalize_text(recipient.get("research_areas")),
                            json.dumps(all_topics, default=str),
                            json.dumps(recent_publications, default=str),
                        )
                    )

                psycopg2.extras.execute_values(
                    cur,
                    f"""
                    INSERT INTO {self.BULK_EMAIL_RECIPIENTS_TABLE}
                        (job_id, status, orcid_id, author_name, email, openalex_id,
                         specialty, research_areas, all_topics_json, recent_publications_json)
                    VALUES %s;
                    """,
                    values,
                    page_size=500,
                )
                return job_id
        except Exception as e:
            print(f"PostgreSQL create bulk email job error: {e}")
            return None

    def _refresh_bulk_job_counts(self, job_id: int) -> bool:
        """Recompute cached counters and finish completed jobs."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT status, COUNT(*) AS cnt
                    FROM {self.BULK_EMAIL_RECIPIENTS_TABLE}
                    WHERE job_id = %s
                    GROUP BY status;
                    """,
                    (job_id,),
                )
                counts = {row["status"]: int(row["cnt"]) for row in cur.fetchall()}
                pending = counts.get(BULK_RECIPIENT_STATUS_PENDING, 0) + counts.get(BULK_RECIPIENT_STATUS_SENDING, 0)
                sent = counts.get(BULK_RECIPIENT_STATUS_SENT, 0)
                failed = counts.get(BULK_RECIPIENT_STATUS_FAILED, 0)
                skipped = counts.get(BULK_RECIPIENT_STATUS_SKIPPED, 0)
                terminal = pending == 0
                cur.execute(
                    f"""
                    UPDATE {self.BULK_EMAIL_JOBS_TABLE}
                    SET pending_count = %s,
                        sent_count = %s,
                        failed_count = %s,
                        skipped_count = %s,
                        status = CASE
                            WHEN cancel_requested AND status <> %s THEN %s
                            WHEN %s AND status NOT IN (%s, %s) THEN %s
                            ELSE status
                        END,
                        completed_at = CASE
                            WHEN (%s OR cancel_requested) AND completed_at IS NULL THEN NOW()
                            ELSE completed_at
                        END,
                        updated_at = NOW()
                    WHERE id = %s;
                    """,
                    (
                        pending,
                        sent,
                        failed,
                        skipped,
                        BULK_JOB_STATUS_CANCELLED,
                        BULK_JOB_STATUS_CANCELLED,
                        terminal,
                        BULK_JOB_STATUS_COMPLETED,
                        BULK_JOB_STATUS_CANCELLED,
                        BULK_JOB_STATUS_COMPLETED,
                        terminal,
                        job_id,
                    ),
                )
            return True
        except Exception as e:
            print(f"PostgreSQL refresh bulk job counts error: {e}")
            return False

    def claim_next_bulk_email_recipient(self) -> Optional[Dict[str, Any]]:
        """Claim the next pending bulk email recipient for a running worker."""
        if not self.available:
            return None
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.BULK_EMAIL_RECIPIENTS_TABLE}
                    SET status = %s,
                        last_error = 'Worker restarted before completing this attempt',
                        updated_at = NOW()
                    WHERE status = %s
                      AND claimed_at IS NOT NULL
                      AND claimed_at < NOW() - INTERVAL '30 minutes';
                    """,
                    (BULK_RECIPIENT_STATUS_PENDING, BULK_RECIPIENT_STATUS_SENDING),
                )
                cur.execute(
                    f"""
                    WITH next_recipient AS (
                        SELECT r.id
                        FROM {self.BULK_EMAIL_RECIPIENTS_TABLE} r
                        JOIN {self.BULK_EMAIL_JOBS_TABLE} j ON j.id = r.job_id
                        WHERE r.status = %s
                          AND j.status IN (%s, %s)
                          AND NOT j.cancel_requested
                        ORDER BY j.created_at ASC, r.id ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE {self.BULK_EMAIL_RECIPIENTS_TABLE} r
                    SET status = %s,
                        attempts = attempts + 1,
                        claimed_at = NOW(),
                        updated_at = NOW()
                    FROM next_recipient
                    WHERE r.id = next_recipient.id
                    RETURNING r.*;
                    """,
                    (
                        BULK_RECIPIENT_STATUS_PENDING,
                        BULK_JOB_STATUS_QUEUED,
                        BULK_JOB_STATUS_RUNNING,
                        BULK_RECIPIENT_STATUS_SENDING,
                    ),
                )
                recipient = cur.fetchone()
                if not recipient:
                    return None
                job_id = int(recipient["job_id"])
                cur.execute(
                    f"""
                    UPDATE {self.BULK_EMAIL_JOBS_TABLE}
                    SET status = %s,
                        started_at = COALESCE(started_at, NOW()),
                        last_recipient = %s,
                        updated_at = NOW()
                    WHERE id = %s AND status <> %s;
                    """,
                    (
                        BULK_JOB_STATUS_RUNNING,
                        recipient.get("author_name") or recipient.get("email") or "",
                        job_id,
                        BULK_JOB_STATUS_CANCELLED,
                    ),
                )
                cur.execute(
                    f"SELECT * FROM {self.BULK_EMAIL_JOBS_TABLE} WHERE id = %s;",
                    (job_id,),
                )
                job = cur.fetchone()
                return {"recipient": dict(recipient), "job": dict(job) if job else {}}
        except Exception as e:
            print(f"PostgreSQL claim bulk email recipient error: {e}")
            return None

    def mark_bulk_email_recipient(
        self,
        recipient_id: int,
        status: str,
        error_message: str = "",
        provider_response: str = "",
    ) -> bool:
        """Record the outcome for one bulk email recipient and refresh its job."""
        if not self.available:
            return False
        if status not in {
            BULK_RECIPIENT_STATUS_PENDING,
            BULK_RECIPIENT_STATUS_SENT,
            BULK_RECIPIENT_STATUS_FAILED,
            BULK_RECIPIENT_STATUS_SKIPPED,
        }:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.BULK_EMAIL_RECIPIENTS_TABLE}
                    SET status = %s,
                        last_error = %s,
                        provider_response = %s,
                        sent_at = CASE WHEN %s = %s THEN NOW() ELSE sent_at END,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING job_id, author_name, email;
                    """,
                    (
                        status,
                        error_message[:1000],
                        provider_response[:1000],
                        status,
                        BULK_RECIPIENT_STATUS_SENT,
                        recipient_id,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    return False
                job_id = int(row["job_id"])
                cur.execute(
                    f"""
                    UPDATE {self.BULK_EMAIL_JOBS_TABLE}
                    SET last_recipient = %s,
                        last_error = CASE WHEN %s <> '' THEN %s ELSE last_error END,
                        last_provider_response = CASE WHEN %s <> '' THEN %s ELSE last_provider_response END,
                        updated_at = NOW()
                    WHERE id = %s;
                    """,
                    (
                        row.get("author_name") or row.get("email") or "",
                        error_message,
                        error_message[:1000],
                        provider_response,
                        provider_response[:1000],
                        job_id,
                    ),
                )
            return self._refresh_bulk_job_counts(job_id)
        except Exception as e:
            print(f"PostgreSQL mark bulk email recipient error: {e}")
            return False

    def retry_bulk_email_recipient(self, recipient_id: int, error_message: str = "") -> bool:
        """Return a recipient to pending for one more attempt."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.BULK_EMAIL_RECIPIENTS_TABLE}
                    SET status = %s,
                        last_error = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING job_id;
                    """,
                    (BULK_RECIPIENT_STATUS_PENDING, error_message[:1000], recipient_id),
                )
                row = cur.fetchone()
                if not row:
                    return False
                job_id = int(row["job_id"])
            return self._refresh_bulk_job_counts(job_id)
        except Exception as e:
            print(f"PostgreSQL retry bulk email recipient error: {e}")
            return False

    def get_recent_bulk_email_jobs(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Return recent bulk email jobs for dashboard display."""
        if not self.available:
            return []
        safe_limit = max(1, min(int(limit or 5), 25))
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT *
                    FROM {self.BULK_EMAIL_JOBS_TABLE}
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (safe_limit,),
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"PostgreSQL get recent bulk email jobs error: {e}")
            return []

    def get_bulk_email_queue_summary(self) -> Dict[str, int]:
        """Return compact queue counts for worker diagnostics."""
        summary = {"active_jobs": 0, "pending_recipients": 0, "sending_recipients": 0}
        if not self.available:
            return summary
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT COUNT(*) AS cnt
                    FROM {self.BULK_EMAIL_JOBS_TABLE}
                    WHERE status IN (%s, %s) AND NOT cancel_requested;
                    """,
                    (BULK_JOB_STATUS_QUEUED, BULK_JOB_STATUS_RUNNING),
                )
                row = cur.fetchone() or {}
                summary["active_jobs"] = int(row.get("cnt") or 0)
                cur.execute(
                    f"""
                    SELECT status, COUNT(*) AS cnt
                    FROM {self.BULK_EMAIL_RECIPIENTS_TABLE}
                    WHERE status IN (%s, %s)
                    GROUP BY status;
                    """,
                    (BULK_RECIPIENT_STATUS_PENDING, BULK_RECIPIENT_STATUS_SENDING),
                )
                for row in cur.fetchall():
                    if row.get("status") == BULK_RECIPIENT_STATUS_PENDING:
                        summary["pending_recipients"] = int(row.get("cnt") or 0)
                    if row.get("status") == BULK_RECIPIENT_STATUS_SENDING:
                        summary["sending_recipients"] = int(row.get("cnt") or 0)
            return summary
        except Exception as e:
            print(f"PostgreSQL bulk email queue summary error: {e}")
            return summary

    def cancel_bulk_email_job(self, job_id: int) -> bool:
        """Cancel a queued/running bulk email job and skip pending recipients."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.BULK_EMAIL_JOBS_TABLE}
                    SET cancel_requested = TRUE,
                        status = %s,
                        completed_at = COALESCE(completed_at, NOW()),
                        updated_at = NOW()
                    WHERE id = %s AND status IN (%s, %s);
                    """,
                    (
                        BULK_JOB_STATUS_CANCELLED,
                        int(job_id),
                        BULK_JOB_STATUS_QUEUED,
                        BULK_JOB_STATUS_RUNNING,
                    ),
                )
                cur.execute(
                    f"""
                    UPDATE {self.BULK_EMAIL_RECIPIENTS_TABLE}
                    SET status = %s,
                        last_error = 'Job cancelled',
                        updated_at = NOW()
                    WHERE job_id = %s AND status = %s;
                    """,
                    (
                        BULK_RECIPIENT_STATUS_SKIPPED,
                        int(job_id),
                        BULK_RECIPIENT_STATUS_PENDING,
                    ),
                )
            return self._refresh_bulk_job_counts(int(job_id))
        except Exception as e:
            print(f"PostgreSQL cancel bulk email job error: {e}")
            return False

    # --- Retraction methods ---

    def get_retracted_names(self) -> Set[str]:
        """Get all unique retracted author names (lowercased) for batch matching."""
        if not self.available:
            return set()
        try:
            with self._get_cursor() as cur:
                cur.execute("SELECT DISTINCT author_name_lower FROM retracted_authors;")
                return {row["author_name_lower"] for row in cur.fetchall()}
        except Exception as e:
            print(f"PostgreSQL get_retracted_names error: {e}")
            return set()

    def is_retracted(self, author_name: str) -> bool:
        """Check if an author name appears in the retraction database."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM retracted_authors WHERE author_name_lower = %s LIMIT 1;",
                    (author_name.lower(),),
                )
                return cur.fetchone() is not None
        except Exception as e:
            print(f"PostgreSQL is_retracted error: {e}")
            return False

    def get_retracted_count(self) -> int:
        """Get total unique retracted author names."""
        if not self.available:
            return 0
        try:
            with self._get_cursor() as cur:
                cur.execute("SELECT COUNT(DISTINCT author_name_lower) AS cnt FROM retracted_authors;")
                row = cur.fetchone()
                return row["cnt"] if row else 0
        except Exception as e:
            print(f"PostgreSQL get_retracted_count error: {e}")
            return 0

    # ------------------------------------------------------------------
    # Background email-collection service
    # ------------------------------------------------------------------
    def get_or_create_run(self) -> Optional[Dict[str, Any]]:
        """Return the singleton collection run row, creating it if absent."""
        if not self.available:
            return None
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {self.COLLECTION_RUNS_TABLE} WHERE id = 1;"
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
                cur.execute(
                    f"""
                    INSERT INTO {self.COLLECTION_RUNS_TABLE} (id, status)
                    VALUES (1, %s)
                    ON CONFLICT (id) DO NOTHING;
                    """,
                    (RUN_STATUS_IDLE,),
                )
                cur.execute(
                    f"SELECT * FROM {self.COLLECTION_RUNS_TABLE} WHERE id = 1;"
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"PostgreSQL get_or_create_run error: {e}")
            return None

    def get_active_run(self) -> Optional[Dict[str, Any]]:
        """Return the current collection run row (alias of get_or_create_run)."""
        return self.get_or_create_run()

    def update_run_state(self, **fields: Any) -> bool:
        """Update arbitrary columns on the singleton collection run row."""
        if not self.available or not fields:
            return False
        allowed = {
            "status", "seed_cursor", "seed_exhausted", "effective_concurrency",
            "effective_delay", "baseline_concurrency", "baseline_delay",
            "last_429_at", "cooldown_until", "stop_until", "run_429_count",
            "clean_batches",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{key} = %s" for key in updates)
        values = list(updates.values())
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_RUNS_TABLE}
                    SET {set_clause}, updated_at = NOW()
                    WHERE id = 1;
                    """,
                    values,
                )
            return True
        except Exception as e:
            print(f"PostgreSQL update_run_state error: {e}")
            return False

    def set_run_status(self, status: str) -> bool:
        """Set the collection run status."""
        return self.update_run_state(status=status)

    def set_run_config(
        self,
        domains: Optional[List[str]] = None,
        disciplines: Optional[List[str]] = None,
        specialties: Optional[List[str]] = None,
        exclude_countries: Optional[List[str]] = None,
        keyword_tags: Optional[str] = None,
        topic_ids: Optional[List[str]] = None,
        h_index_min: Optional[int] = None,
        h_index_max: Optional[int] = None,
        baseline_concurrency: Optional[int] = None,
        baseline_delay: Optional[float] = None,
        reset_cursor: bool = False,
    ) -> bool:
        """Persist run filters and pacing; optionally reset the OpenAlex cursor."""
        if not self.available:
            return False
        self.get_or_create_run()
        updates: Dict[str, Any] = {}
        if domains is not None:
            updates["domains_json"] = json.dumps(domains)
        if disciplines is not None:
            updates["disciplines_json"] = json.dumps(disciplines)
        if specialties is not None:
            updates["specialties_json"] = json.dumps(specialties)
        if exclude_countries is not None:
            updates["exclude_countries_json"] = json.dumps(exclude_countries)
        if keyword_tags is not None:
            updates["keyword_tags"] = keyword_tags
        if topic_ids is not None:
            updates["topic_ids_json"] = json.dumps(topic_ids)
        if h_index_min is not None:
            updates["h_index_min"] = int(h_index_min)
        if h_index_max is not None:
            updates["h_index_max"] = int(h_index_max)
        if baseline_concurrency is not None:
            updates["baseline_concurrency"] = int(baseline_concurrency)
        if baseline_delay is not None:
            updates["baseline_delay"] = float(baseline_delay)
        if reset_cursor:
            updates["seed_cursor"] = "*"
            updates["seed_exhausted"] = False
        if not updates:
            return False
        set_clause = ", ".join(f"{key} = %s" for key in updates)
        values = list(updates.values())
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_RUNS_TABLE}
                    SET {set_clause}, updated_at = NOW()
                    WHERE id = 1;
                    """,
                    values,
                )
            return True
        except Exception as e:
            print(f"PostgreSQL set_run_config error: {e}")
            return False

    def upsert_harvested_author(self, author: Dict[str, Any], run_id: int = 1) -> bool:
        """Upsert one harvested author with full metadata, preserving email state."""
        return self.bulk_upsert_harvested_authors([author], run_id=run_id) > 0

    def bulk_upsert_harvested_authors(
        self, authors: List[Dict[str, Any]], run_id: int = 1
    ) -> int:
        """Bulk-upsert harvested authors keyed by openalex_id; returns rows written."""
        if not self.available or not authors:
            return 0
        written = 0
        try:
            with self._get_cursor() as cur:
                for author in authors:
                    openalex_id = _normalize_openalex_id(author.get("author_id"))
                    if not openalex_id:
                        continue
                    orcid_id = _normalize_orcid(author.get("orcid_id"))
                    name = _normalize_text(author.get("name"))
                    name_lower = _normalize_author_name(author.get("name"))
                    initial_status = (
                        EMAIL_STATUS_PENDING if orcid_id else EMAIL_STATUS_NO_ORCID
                    )
                    all_topics = author.get("all_topics") or []
                    try:
                        all_topics_json = json.dumps(all_topics)
                    except Exception:
                        all_topics_json = "[]"
                    cur.execute(
                        f"""
                        INSERT INTO {self.HARVESTED_AUTHORS_TABLE}
                            (openalex_id, orcid_id, author_name, author_name_lower,
                             h_index, works_count, cited_by_count, institution, country,
                             discipline, specialty, subfield, research_areas, all_topics_json,
                             email_status, run_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (openalex_id) DO UPDATE SET
                            orcid_id = CASE WHEN EXCLUDED.orcid_id <> '' THEN EXCLUDED.orcid_id
                                            ELSE {self.HARVESTED_AUTHORS_TABLE}.orcid_id END,
                            author_name = CASE WHEN EXCLUDED.author_name <> '' THEN EXCLUDED.author_name
                                               ELSE {self.HARVESTED_AUTHORS_TABLE}.author_name END,
                            author_name_lower = CASE WHEN EXCLUDED.author_name_lower <> '' THEN EXCLUDED.author_name_lower
                                                     ELSE {self.HARVESTED_AUTHORS_TABLE}.author_name_lower END,
                            h_index = COALESCE(EXCLUDED.h_index, {self.HARVESTED_AUTHORS_TABLE}.h_index),
                            works_count = COALESCE(EXCLUDED.works_count, {self.HARVESTED_AUTHORS_TABLE}.works_count),
                            cited_by_count = COALESCE(EXCLUDED.cited_by_count, {self.HARVESTED_AUTHORS_TABLE}.cited_by_count),
                            institution = CASE WHEN EXCLUDED.institution <> '' THEN EXCLUDED.institution
                                               ELSE {self.HARVESTED_AUTHORS_TABLE}.institution END,
                            country = CASE WHEN EXCLUDED.country <> '' THEN EXCLUDED.country
                                           ELSE {self.HARVESTED_AUTHORS_TABLE}.country END,
                            discipline = CASE WHEN EXCLUDED.discipline <> '' THEN EXCLUDED.discipline
                                              ELSE {self.HARVESTED_AUTHORS_TABLE}.discipline END,
                            specialty = CASE WHEN EXCLUDED.specialty <> '' THEN EXCLUDED.specialty
                                             ELSE {self.HARVESTED_AUTHORS_TABLE}.specialty END,
                            subfield = CASE WHEN EXCLUDED.subfield <> '' THEN EXCLUDED.subfield
                                            ELSE {self.HARVESTED_AUTHORS_TABLE}.subfield END,
                            research_areas = CASE WHEN EXCLUDED.research_areas <> '' THEN EXCLUDED.research_areas
                                                  ELSE {self.HARVESTED_AUTHORS_TABLE}.research_areas END,
                            all_topics_json = CASE WHEN EXCLUDED.all_topics_json <> '[]' THEN EXCLUDED.all_topics_json
                                                   ELSE {self.HARVESTED_AUTHORS_TABLE}.all_topics_json END,
                            updated_at = NOW();
                        """,
                        (
                            openalex_id,
                            orcid_id,
                            name,
                            name_lower,
                            author.get("h_index"),
                            author.get("works_count"),
                            author.get("cited_by_count"),
                            _normalize_text(author.get("institution")),
                            _normalize_text(author.get("country")),
                            _normalize_text(author.get("discipline")),
                            _normalize_text(author.get("specialty")),
                            _normalize_text(author.get("subfield")),
                            _normalize_text(author.get("research_areas")),
                            all_topics_json,
                            initial_status,
                            int(run_id),
                        ),
                    )
                    written += 1
            return written
        except Exception as e:
            print(f"PostgreSQL bulk_upsert_harvested_authors error: {e}")
            return written

    def get_pending_harvest(
        self, limit: int = 50, require_orcid: bool = True
    ) -> List[Dict[str, Any]]:
        """Return pending harvested authors due for an email lookup."""
        if not self.available:
            return []
        safe_limit = max(1, min(int(limit or 50), 1000))
        orcid_clause = "AND orcid_id <> ''" if require_orcid else ""
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT * FROM {self.HARVESTED_AUTHORS_TABLE}
                    WHERE email_status = %s {orcid_clause}
                      AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                    ORDER BY next_retry_at ASC NULLS FIRST, created_at ASC
                    LIMIT %s;
                    """,
                    (EMAIL_STATUS_PENDING, safe_limit),
                )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"PostgreSQL get_pending_harvest error: {e}")
            return []

    def update_harvest_email(
        self,
        openalex_id: str,
        email: str = "",
        status: str = EMAIL_STATUS_NO_EMAIL,
        email_source: str = "",
        all_emails: str = "",
        next_retry_at: Optional[datetime] = None,
    ) -> bool:
        """Record the outcome of an email lookup for a harvested author."""
        if not self.available:
            return False
        normalized_id = _normalize_openalex_id(openalex_id)
        if not normalized_id:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.HARVESTED_AUTHORS_TABLE}
                    SET email = CASE WHEN %s <> '' THEN %s ELSE email END,
                        all_emails = CASE WHEN %s <> '' THEN %s ELSE all_emails END,
                        email_source = CASE WHEN %s <> '' THEN %s ELSE email_source END,
                        email_status = %s,
                        attempts = attempts + 1,
                        last_checked_at = NOW(),
                        next_retry_at = %s,
                        updated_at = NOW()
                    WHERE openalex_id = %s;
                    """,
                    (
                        email, email,
                        all_emails, all_emails,
                        email_source, email_source,
                        status,
                        next_retry_at,
                        normalized_id,
                    ),
                )
            return True
        except Exception as e:
            print(f"PostgreSQL update_harvest_email error: {e}")
            return False

    def count_harvest_by_status(self) -> Dict[str, int]:
        """Return counts of harvested authors grouped by email_status."""
        counts: Dict[str, int] = {}
        if not self.available:
            return counts
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT email_status, COUNT(*) AS cnt
                    FROM {self.HARVESTED_AUTHORS_TABLE}
                    GROUP BY email_status;
                    """
                )
                for row in cur.fetchall():
                    counts[row["email_status"]] = int(row["cnt"])
            return counts
        except Exception as e:
            print(f"PostgreSQL count_harvest_by_status error: {e}")
            return counts

    def bump_daily_stat(self, field: str, increment: int = 1, day: Optional[Any] = None) -> bool:
        """Increment a counter in collection_daily_stats for the given UTC day."""
        if not self.available:
            return False
        allowed = {"emails_found", "attempts", "orcid_429", "openalex_429", "seeded"}
        if field not in allowed:
            return False
        target_day = day or datetime.now(timezone.utc).date()
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.COLLECTION_DAILY_STATS_TABLE} (day, {field})
                    VALUES (%s, %s)
                    ON CONFLICT (day) DO UPDATE SET
                        {field} = {self.COLLECTION_DAILY_STATS_TABLE}.{field} + EXCLUDED.{field};
                    """,
                    (target_day, int(increment)),
                )
            return True
        except Exception as e:
            print(f"PostgreSQL bump_daily_stat error: {e}")
            return False

    def get_recent_harvested(
        self, limit: int = 25, status: str = EMAIL_STATUS_FOUND
    ) -> List[Dict[str, Any]]:
        """Return the most recently updated harvested authors for a given status."""
        if not self.available:
            return []
        safe_limit = max(1, min(int(limit or 25), 500))
        try:
            with self._get_cursor() as cur:
                if status:
                    cur.execute(
                        f"""
                        SELECT * FROM {self.HARVESTED_AUTHORS_TABLE}
                        WHERE email_status = %s
                        ORDER BY updated_at DESC
                        LIMIT %s;
                        """,
                        (status, safe_limit),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT * FROM {self.HARVESTED_AUTHORS_TABLE}
                        ORDER BY updated_at DESC
                        LIMIT %s;
                        """,
                        (safe_limit,),
                    )
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"PostgreSQL get_recent_harvested error: {e}")
            return []

    def get_collection_summary(self) -> Dict[str, Any]:
        """Aggregate live status for the collection dashboard."""
        summary: Dict[str, Any] = {
            "run": None,
            "status_counts": {},
            "queue_pending": 0,
            "total_collected": 0,
            "emails_found_today": 0,
            "attempts_today": 0,
            "orcid_429_today": 0,
            "hit_rate": 0.0,
        }
        if not self.available:
            return summary
        try:
            summary["run"] = self.get_or_create_run()
            counts = self.count_harvest_by_status()
            summary["status_counts"] = counts
            summary["queue_pending"] = counts.get(EMAIL_STATUS_PENDING, 0)
            summary["total_collected"] = counts.get(EMAIL_STATUS_FOUND, 0)
            today = datetime.now(timezone.utc).date()
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT emails_found, attempts, orcid_429
                    FROM {self.COLLECTION_DAILY_STATS_TABLE}
                    WHERE day = %s;
                    """,
                    (today,),
                )
                row = cur.fetchone()
            if row:
                summary["emails_found_today"] = int(row.get("emails_found", 0) or 0)
                summary["attempts_today"] = int(row.get("attempts", 0) or 0)
                summary["orcid_429_today"] = int(row.get("orcid_429", 0) or 0)
                if summary["attempts_today"] > 0:
                    summary["hit_rate"] = round(
                        summary["emails_found_today"] / summary["attempts_today"], 4
                    )
            return summary
        except Exception as e:
            print(f"PostgreSQL get_collection_summary error: {e}")
            return summary


# Singleton instance
_storage: Optional[PostgresStorage] = None


def get_storage() -> PostgresStorage:
    """Get or create PostgreSQL storage singleton."""
    global _storage
    if _storage is None:
        _storage = PostgresStorage()
    return _storage
