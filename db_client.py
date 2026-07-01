"""PostgreSQL client for persistent storage of sent invitations (Railway)."""

import os
import json
import re
import secrets
import hashlib
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
EMAIL_SUPPRESSION_SOURCE_UNSUBSCRIBE = "unsubscribe_link"


def _normalize_collection_list(values: Optional[List[Any]], *, upper: bool = False) -> List[str]:
    """Return stable, order-insensitive collection-filter values."""
    normalized = set()
    for value in values or []:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text:
            continue
        normalized.add(text.upper() if upper else text.casefold())
    return sorted(normalized)


def normalize_collection_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Canonicalize all targeting fields used to identify a resumable search."""
    config = config or {}
    keyword_tags = _normalize_collection_list(
        re.split(r"[,\n]", str(config.get("keyword_tags") or ""))
    )
    topic_ids = []
    for value in config.get("topic_ids") or []:
        topic_id = str(value or "").strip().rstrip("/").split("/")[-1].upper()
        if topic_id:
            topic_ids.append(topic_id)
    return {
        "domains": _normalize_collection_list(config.get("domains")),
        "disciplines": _normalize_collection_list(config.get("disciplines")),
        "specialties": _normalize_collection_list(config.get("specialties")),
        "exclude_countries": _normalize_collection_list(
            config.get("exclude_countries"), upper=True
        ),
        "keyword_tags": keyword_tags,
        "topic_ids": sorted(set(topic_ids)),
        "h_index_min": int(config.get("h_index_min") or 0),
        "h_index_max": int(config.get("h_index_max") or 0),
    }


def build_collection_search_key(config: Optional[Dict[str, Any]]) -> str:
    """Build a deterministic fingerprint for resume/start decisions."""
    payload = json.dumps(
        normalize_collection_config(config),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(value or "[]")
        return decoded if isinstance(decoded, list) else []
    except (TypeError, ValueError):
        return []


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
    COLLECTION_SEARCH_RUNS_TABLE = "collection_search_runs"
    COLLECTION_RUN_AUTHORS_TABLE = "collection_run_authors"
    HARVESTED_AUTHORS_TABLE = "harvested_authors"
    COLLECTION_DAILY_STATS_TABLE = "collection_daily_stats"
    BULK_EMAIL_JOBS_TABLE = "bulk_email_jobs"
    BULK_EMAIL_RECIPIENTS_TABLE = "bulk_email_recipients"
    JOURNAL_PRESETS_TABLE = "journal_presets"
    EMAIL_SUPPRESSIONS_TABLE = "email_suppressions"

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
        with self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.EMAIL_SUPPRESSIONS_TABLE} (
                    id               SERIAL PRIMARY KEY,
                    email_lower      TEXT NOT NULL UNIQUE,
                    orcid_id         TEXT DEFAULT '',
                    profile_key      TEXT DEFAULT '',
                    unsubscribe_token TEXT NOT NULL UNIQUE,
                    is_suppressed    BOOLEAN DEFAULT FALSE,
                    reason           TEXT DEFAULT '',
                    source           TEXT DEFAULT '{EMAIL_SUPPRESSION_SOURCE_UNSUBSCRIBE}',
                    suppressed_at    TIMESTAMPTZ DEFAULT NOW(),
                    updated_at       TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute(f"""
                ALTER TABLE {self.EMAIL_SUPPRESSIONS_TABLE}
                ADD COLUMN IF NOT EXISTS is_suppressed BOOLEAN DEFAULT FALSE;
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_email_suppressions_orcid
                ON {self.EMAIL_SUPPRESSIONS_TABLE} (orcid_id);
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_email_suppressions_profile_key
                ON {self.EMAIL_SUPPRESSIONS_TABLE} (profile_key);
            """)
            self._ensure_collection_tables(cur)
            self._ensure_bulk_email_tables(cur)
            self._ensure_journal_preset_tables(cur)

    def _ensure_journal_preset_tables(self, cur):
        """Create tables for saved journal invitation presets."""
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.JOURNAL_PRESETS_TABLE} (
                id                  SERIAL PRIMARY KEY,
                preset_name         TEXT NOT NULL UNIQUE,
                publisher_id        TEXT DEFAULT '',
                journal_config_json TEXT DEFAULT '{{}}',
                created_at          TIMESTAMPTZ DEFAULT NOW(),
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_presets_name_unique
            ON {self.JOURNAL_PRESETS_TABLE} (LOWER(preset_name));
        """)

    def _decode_journal_preset_row(self, row) -> Dict[str, Any]:
        """Decode a journal preset row from PostgreSQL into app-friendly data."""
        preset = dict(row)
        raw_config = preset.pop("journal_config_json", "") or "{}"
        try:
            preset["journal_config"] = json.loads(raw_config)
        except (TypeError, json.JSONDecodeError):
            preset["journal_config"] = {}
        return preset

    def list_journal_presets(self) -> List[Dict[str, Any]]:
        """Return saved journal presets ordered by name."""
        if not self.available:
            return []
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, preset_name, publisher_id, journal_config_json, created_at, updated_at
                    FROM {self.JOURNAL_PRESETS_TABLE}
                    ORDER BY LOWER(preset_name), id;
                    """
                )
                return [self._decode_journal_preset_row(row) for row in cur.fetchall()]
        except Exception as e:
            print(f"PostgreSQL list journal presets error: {e}")
            return []

    def get_journal_preset(self, preset_id: int) -> Optional[Dict[str, Any]]:
        """Return one saved journal preset by id."""
        if not self.available:
            return None
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, preset_name, publisher_id, journal_config_json, created_at, updated_at
                    FROM {self.JOURNAL_PRESETS_TABLE}
                    WHERE id = %s;
                    """,
                    (int(preset_id),),
                )
                row = cur.fetchone()
                return self._decode_journal_preset_row(row) if row else None
        except Exception as e:
            print(f"PostgreSQL get journal preset error: {e}")
            return None

    def create_journal_preset(
        self,
        preset_name: str,
        publisher_id: str,
        journal_config: Dict[str, Any],
    ) -> Optional[int]:
        """Create a reusable journal preset and return its id."""
        if not self.available:
            return None
        clean_name = _normalize_text(preset_name)
        if not clean_name:
            return None
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.JOURNAL_PRESETS_TABLE}
                        (preset_name, publisher_id, journal_config_json, created_at, updated_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                    RETURNING id;
                    """,
                    (
                        clean_name,
                        _normalize_text(publisher_id),
                        json.dumps(journal_config or {}, ensure_ascii=False),
                    ),
                )
                row = cur.fetchone()
                return int(row["id"]) if row else None
        except Exception as e:
            print(f"PostgreSQL create journal preset error: {e}")
            return None

    def update_journal_preset(
        self,
        preset_id: int,
        preset_name: str,
        publisher_id: str,
        journal_config: Dict[str, Any],
    ) -> bool:
        """Update an existing reusable journal preset."""
        if not self.available:
            return False
        clean_name = _normalize_text(preset_name)
        if not clean_name:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.JOURNAL_PRESETS_TABLE}
                    SET preset_name = %s,
                        publisher_id = %s,
                        journal_config_json = %s,
                        updated_at = NOW()
                    WHERE id = %s;
                    """,
                    (
                        clean_name,
                        _normalize_text(publisher_id),
                        json.dumps(journal_config or {}, ensure_ascii=False),
                        int(preset_id),
                    ),
                )
                return cur.rowcount > 0
        except Exception as e:
            print(f"PostgreSQL update journal preset error: {e}")
            return False

    def delete_journal_preset(self, preset_id: int) -> bool:
        """Delete a reusable journal preset."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"DELETE FROM {self.JOURNAL_PRESETS_TABLE} WHERE id = %s;",
                    (int(preset_id),),
                )
                return cur.rowcount > 0
        except Exception as e:
            print(f"PostgreSQL delete journal preset error: {e}")
            return False

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
            ALTER TABLE {self.COLLECTION_RUNS_TABLE}
            ADD COLUMN IF NOT EXISTS active_search_run_id BIGINT;
        """)
        cur.execute(f"""
            ALTER TABLE {self.COLLECTION_RUNS_TABLE}
            ADD COLUMN IF NOT EXISTS worker_owner TEXT DEFAULT '';
        """)
        cur.execute(f"""
            ALTER TABLE {self.COLLECTION_RUNS_TABLE}
            ADD COLUMN IF NOT EXISTS worker_lease_until TIMESTAMPTZ;
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.COLLECTION_SEARCH_RUNS_TABLE} (
                id                      BIGSERIAL PRIMARY KEY,
                search_key              TEXT NOT NULL,
                generation              INTEGER NOT NULL DEFAULT 1,
                domains_json            TEXT DEFAULT '[]',
                disciplines_json        TEXT DEFAULT '[]',
                specialties_json        TEXT DEFAULT '[]',
                exclude_countries_json  TEXT DEFAULT '[]',
                keyword_tags            TEXT DEFAULT '',
                topic_ids_json          TEXT DEFAULT '[]',
                selected_topic_ids_json TEXT DEFAULT '[]',
                topic_details_json      TEXT DEFAULT '[]',
                h_index_min             INTEGER,
                h_index_max             INTEGER,
                seed_cursor             TEXT DEFAULT '*',
                seed_exhausted          BOOLEAN DEFAULT FALSE,
                emails_found            INTEGER DEFAULT 0,
                attempts                INTEGER DEFAULT 0,
                orcid_429               INTEGER DEFAULT 0,
                openalex_429             INTEGER DEFAULT 0,
                seeded                  INTEGER DEFAULT 0,
                created_at              TIMESTAMPTZ DEFAULT NOW(),
                updated_at              TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (search_key, generation)
            );
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_collection_search_resume
            ON {self.COLLECTION_SEARCH_RUNS_TABLE} (search_key, generation DESC);
        """)
        cur.execute(f"""
            ALTER TABLE {self.COLLECTION_SEARCH_RUNS_TABLE}
            ADD COLUMN IF NOT EXISTS selected_topic_ids_json TEXT DEFAULT '[]';
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
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.COLLECTION_RUN_AUTHORS_TABLE} (
                search_run_id BIGINT NOT NULL
                    REFERENCES {self.COLLECTION_SEARCH_RUNS_TABLE}(id) ON DELETE CASCADE,
                openalex_id TEXT NOT NULL
                    REFERENCES {self.HARVESTED_AUTHORS_TABLE}(openalex_id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (search_run_id, openalex_id)
            );
        """)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_collection_run_authors_openalex
            ON {self.COLLECTION_RUN_AUTHORS_TABLE} (openalex_id, search_run_id);
        """)

        # One-time additive migration of the legacy singleton checkpoint. This is
        # intentionally idempotent so frontend and worker may start concurrently.
        cur.execute(f"SELECT * FROM {self.COLLECTION_RUNS_TABLE} WHERE id = 1;")
        legacy = cur.fetchone()
        if legacy:
            legacy = dict(legacy)
            config = {
                "domains": _decode_json_list(legacy.get("domains_json")),
                "disciplines": _decode_json_list(legacy.get("disciplines_json")),
                "specialties": _decode_json_list(legacy.get("specialties_json")),
                "exclude_countries": _decode_json_list(legacy.get("exclude_countries_json")),
                "keyword_tags": legacy.get("keyword_tags") or "",
                "topic_ids": _decode_json_list(legacy.get("topic_ids_json")),
                "h_index_min": legacy.get("h_index_min"),
                "h_index_max": legacy.get("h_index_max"),
            }
            search_key = build_collection_search_key(config)
            cur.execute(
                f"""
                INSERT INTO {self.COLLECTION_SEARCH_RUNS_TABLE}
                    (search_key, generation, domains_json, disciplines_json,
                     specialties_json, exclude_countries_json, keyword_tags,
                     topic_ids_json, selected_topic_ids_json, h_index_min, h_index_max, seed_cursor,
                     seed_exhausted, created_at, updated_at)
                VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, NOW()), COALESCE(%s, NOW()))
                ON CONFLICT (search_key, generation) DO NOTHING
                RETURNING id;
                """,
                (
                    search_key,
                    legacy.get("domains_json") or "[]",
                    legacy.get("disciplines_json") or "[]",
                    legacy.get("specialties_json") or "[]",
                    legacy.get("exclude_countries_json") or "[]",
                    legacy.get("keyword_tags") or "",
                    legacy.get("topic_ids_json") or "[]",
                    legacy.get("topic_ids_json") or "[]",
                    legacy.get("h_index_min"),
                    legacy.get("h_index_max"),
                    legacy.get("seed_cursor") or "*",
                    bool(legacy.get("seed_exhausted")),
                    legacy.get("created_at"),
                    legacy.get("updated_at"),
                ),
            )
            inserted = cur.fetchone()
            was_inserted = bool(inserted)
            if inserted:
                search_run_id = inserted["id"] if isinstance(inserted, dict) else inserted[0]
            else:
                cur.execute(
                    f"""SELECT id FROM {self.COLLECTION_SEARCH_RUNS_TABLE}
                        WHERE search_key = %s AND generation = 1;""",
                    (search_key,),
                )
                found = cur.fetchone()
                search_run_id = (found["id"] if isinstance(found, dict) else found[0]) if found else None
            if search_run_id:
                if was_inserted:
                    cur.execute(
                        f"""
                        UPDATE {self.COLLECTION_SEARCH_RUNS_TABLE} s
                        SET emails_found = d.emails_found,
                            attempts = d.attempts,
                            orcid_429 = d.orcid_429,
                            openalex_429 = d.openalex_429,
                            seeded = d.seeded
                        FROM {self.COLLECTION_DAILY_STATS_TABLE} d
                        WHERE s.id = %s AND d.day = (NOW() AT TIME ZONE 'UTC')::date;
                        """,
                        (search_run_id,),
                    )
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_RUNS_TABLE}
                    SET active_search_run_id = COALESCE(active_search_run_id, %s)
                    WHERE id = 1;
                    """,
                    (search_run_id,),
                )
                cur.execute(
                    f"SELECT 1 FROM {self.COLLECTION_RUN_AUTHORS_TABLE} WHERE search_run_id = %s LIMIT 1;",
                    (search_run_id,),
                )
                if not cur.fetchone():
                    cur.execute(
                        f"""
                        INSERT INTO {self.COLLECTION_RUN_AUTHORS_TABLE} (search_run_id, openalex_id)
                        SELECT %s, openalex_id
                        FROM {self.HARVESTED_AUTHORS_TABLE}
                        WHERE run_id = 1
                        ON CONFLICT DO NOTHING;
                        """,
                        (search_run_id,),
                    )

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

    def search_database_email_recipients(
        self,
        query: str = "",
        source: str = "all",
        limit: int = 0,
        require_email: bool = True,
        hide_suppressed: bool = True,
    ) -> List[Dict[str, Any]]:
        """Search email-bearing recipient records across profile and harvested tables."""
        if not self.available:
            return []

        requested_limit = max(0, int(limit or 0))
        limit_clause = " LIMIT %s" if requested_limit > 0 else ""
        normalized_source = (source or "all").strip().lower()
        if normalized_source not in {"all", "profiles", "harvested"}:
            normalized_source = "all"

        terms = [term.strip().lower() for term in re.split(r"\s+", query or "") if term.strip()]
        like_terms = [f"%{term}%" for term in terms]

        def _term_clause(columns: List[str]) -> str:
            if not terms:
                return "TRUE"
            per_term = []
            for _ in terms:
                per_term.append("(" + " OR ".join(f"LOWER(COALESCE({column}, '')) LIKE %s" for column in columns) + ")")
            return " AND ".join(per_term)

        def _term_params(columns: List[str]) -> List[str]:
            params: List[str] = []
            for like_term in like_terms:
                params.extend([like_term] * len(columns))
            return params

        rows: List[Dict[str, Any]] = []
        try:
            with self._get_cursor() as cur:
                if normalized_source in {"all", "profiles"}:
                    profile_columns = [
                        "profile_key",
                        "orcid_id",
                        "openalex_id",
                        "author_name",
                        "email",
                        "email_domain",
                        "scientific_domain",
                        "scientific_domains_json",
                        "publisher",
                        "source",
                    ]
                    profile_params = [bool(require_email), *_term_params(profile_columns), bool(hide_suppressed)]
                    if requested_limit > 0:
                        profile_params.append(requested_limit)
                    cur.execute(
                        f"""
                        SELECT
                            'profiles' AS source_table,
                            profile_key,
                            orcid_id,
                            openalex_id,
                            author_name,
                            email,
                            '' AS all_emails,
                            '' AS institution,
                            '' AS country,
                            scientific_domain AS discipline,
                            '' AS specialty,
                            '' AS subfield,
                            scientific_domain,
                            scientific_domains_json,
                            '' AS research_areas,
                            0 AS h_index,
                            0 AS works_count,
                            0 AS cited_by_count,
                            invitation_count_total,
                            invitation_count_editorial,
                            invitation_count_publication,
                            last_invited_at,
                            updated_at
                        FROM {self.PROFILE_TABLE_NAME} p
                        WHERE (%s = FALSE OR (email <> '' AND email IS NOT NULL))
                          AND ({_term_clause(profile_columns)})
                          AND (
                              %s = FALSE
                              OR NOT EXISTS (
                                  SELECT 1
                                  FROM {self.EMAIL_SUPPRESSIONS_TABLE} s
                                  WHERE s.is_suppressed = TRUE
                                    AND (
                                        s.email_lower = p.email_lower
                                        OR (s.orcid_id <> '' AND s.orcid_id = p.orcid_id)
                                        OR (s.profile_key <> '' AND s.profile_key = p.profile_key)
                                    )
                              )
                          )
                        ORDER BY updated_at DESC
                        {limit_clause};
                        """,
                        profile_params,
                    )
                    rows.extend(dict(row) for row in cur.fetchall())

                remaining = max(requested_limit - len(rows), 0) if requested_limit > 0 else 0
                if (requested_limit == 0 or remaining > 0) and normalized_source in {"all", "harvested"}:
                    harvested_columns = [
                        "openalex_id",
                        "orcid_id",
                        "author_name",
                        "email",
                        "all_emails",
                        "institution",
                        "country",
                        "discipline",
                        "specialty",
                        "subfield",
                        "research_areas",
                        "all_topics_json",
                    ]
                    harvested_limit_clause = " LIMIT %s" if requested_limit > 0 else ""
                    harvested_params = [bool(require_email), *_term_params(harvested_columns), bool(hide_suppressed)]
                    if requested_limit > 0:
                        harvested_params.append(remaining)
                    cur.execute(
                        f"""
                        SELECT
                            'harvested' AS source_table,
                            '' AS profile_key,
                            orcid_id,
                            openalex_id,
                            author_name,
                            email,
                            all_emails,
                            institution,
                            country,
                            discipline,
                            specialty,
                            subfield,
                            discipline AS scientific_domain,
                            all_topics_json AS scientific_domains_json,
                            research_areas,
                            COALESCE(h_index, 0) AS h_index,
                            COALESCE(works_count, 0) AS works_count,
                            COALESCE(cited_by_count, 0) AS cited_by_count,
                            0 AS invitation_count_total,
                            0 AS invitation_count_editorial,
                            0 AS invitation_count_publication,
                            NULL AS last_invited_at,
                            updated_at
                        FROM {self.HARVESTED_AUTHORS_TABLE} h
                        WHERE (%s = FALSE OR (email <> '' AND email IS NOT NULL))
                          AND ({_term_clause(harvested_columns)})
                          AND (
                              %s = FALSE
                              OR NOT EXISTS (
                                  SELECT 1
                                  FROM {self.EMAIL_SUPPRESSIONS_TABLE} s
                                  WHERE s.is_suppressed = TRUE
                                    AND (
                                        s.email_lower = LOWER(h.email)
                                        OR (s.orcid_id <> '' AND s.orcid_id = h.orcid_id)
                                    )
                              )
                          )
                        ORDER BY updated_at DESC
                        {harvested_limit_clause};
                        """,
                        harvested_params,
                    )
                    rows.extend(dict(row) for row in cur.fetchall())
            return rows[:requested_limit] if requested_limit > 0 else rows
        except Exception as e:
            print(f"PostgreSQL search database email recipients error: {e}")
            return []

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

    def get_email_suppression_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Look up a suppression record by unsubscribe token."""
        if not self.available or not token:
            return None
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT *
                    FROM {self.EMAIL_SUPPRESSIONS_TABLE}
                    WHERE unsubscribe_token = %s
                    LIMIT 1;
                    """,
                    (token,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"PostgreSQL get email suppression by token error: {e}")
            return None

    def is_email_suppressed(self, email: str) -> bool:
        """Check whether a normalized email has an active suppression record."""
        if not self.available:
            return False
        normalized_email = _normalize_email(email)
        if not normalized_email:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT 1
                    FROM {self.EMAIL_SUPPRESSIONS_TABLE}
                    WHERE email_lower = %s
                      AND is_suppressed = TRUE
                    LIMIT 1;
                    """,
                    (normalized_email,),
                )
                return cur.fetchone() is not None
        except Exception as e:
            print(f"PostgreSQL is_email_suppressed error: {e}")
            return False

    def is_recipient_suppressed(
        self,
        email: str,
        orcid_id: str = "",
        profile_key: str = "",
    ) -> bool:
        """Check whether a recipient should be suppressed from future sends."""
        if not self.available:
            return False
        normalized_email = _normalize_email(email)
        normalized_orcid = _normalize_orcid(orcid_id)
        normalized_profile_key = _normalize_text(profile_key)
        if not normalized_email and not normalized_orcid and not normalized_profile_key:
            return False

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT 1
                    FROM {self.EMAIL_SUPPRESSIONS_TABLE}
                    WHERE (email_lower = %s AND is_suppressed = TRUE)
                       OR (%s <> '' AND orcid_id = %s AND is_suppressed = TRUE)
                       OR (%s <> '' AND profile_key = %s AND is_suppressed = TRUE)
                    LIMIT 1;
                    """,
                    (
                        normalized_email,
                        normalized_orcid,
                        normalized_orcid,
                        normalized_profile_key,
                        normalized_profile_key,
                    ),
                )
                return cur.fetchone() is not None
        except Exception as e:
            print(f"PostgreSQL is_recipient_suppressed error: {e}")
            return False

    def get_suppressed_recipient_keys(self, recipients: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
        """Resolve suppression state for a result set with one database query."""
        result: Dict[str, Set[str]] = {
            "emails": set(),
            "orcids": set(),
            "profile_keys": set(),
        }
        if not self.available or not recipients:
            return result
        emails = sorted({
            _normalize_email(row.get("email")) for row in recipients
            if _normalize_email(row.get("email"))
        }) or [""]
        orcids = sorted({
            _normalize_orcid(row.get("orcid_id")) for row in recipients
            if _normalize_orcid(row.get("orcid_id"))
        }) or [""]
        profile_keys = sorted({
            _normalize_text(row.get("profile_key")) for row in recipients
            if _normalize_text(row.get("profile_key"))
        }) or [""]
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT email_lower, orcid_id, profile_key
                    FROM {self.EMAIL_SUPPRESSIONS_TABLE}
                    WHERE is_suppressed = TRUE
                      AND (email_lower = ANY(%s)
                           OR orcid_id = ANY(%s)
                           OR profile_key = ANY(%s));
                    """,
                    (emails, orcids, profile_keys),
                )
                for row in cur.fetchall():
                    if row.get("email_lower"):
                        result["emails"].add(_normalize_email(row["email_lower"]))
                    if row.get("orcid_id"):
                        result["orcids"].add(_normalize_orcid(row["orcid_id"]))
                    if row.get("profile_key"):
                        result["profile_keys"].add(_normalize_text(row["profile_key"]))
            return result
        except Exception as e:
            print(f"PostgreSQL bulk suppression lookup error: {e}")
            return result

    def _collect_suppression_identifiers(
        self,
        cur,
        email: str,
        orcid_id: str = "",
        profile_key: str = "",
    ) -> Dict[str, Set[str]]:
        """Collect known identifiers before purging personal mailing data."""
        identifiers: Dict[str, Set[str]] = {
            "emails": set(),
            "orcids": set(),
            "profile_keys": set(),
            "openalex_ids": set(),
        }
        normalized_email = _normalize_email(email)
        normalized_orcid = _normalize_orcid(orcid_id)
        normalized_profile_key = _normalize_text(profile_key)
        if normalized_email:
            identifiers["emails"].add(normalized_email)
        if normalized_orcid:
            identifiers["orcids"].add(normalized_orcid)
        if normalized_profile_key:
            identifiers["profile_keys"].add(normalized_profile_key)

        # Iterate a few times so identifiers found in one table can expose matches in another.
        for _ in range(3):
            before = sum(len(values) for values in identifiers.values())
            emails = sorted(identifiers["emails"]) or [""]
            orcids = sorted(identifiers["orcids"]) or [""]
            profile_keys = sorted(identifiers["profile_keys"]) or [""]
            openalex_ids = sorted(identifiers["openalex_ids"]) or [""]

            cur.execute(
                f"""
                SELECT profile_key, orcid_id, openalex_id, email_lower, email
                FROM {self.PROFILE_TABLE_NAME}
                WHERE email_lower = ANY(%s)
                   OR email = ANY(%s)
                   OR orcid_id = ANY(%s)
                   OR profile_key = ANY(%s)
                   OR openalex_id = ANY(%s);
                """,
                (emails, emails, orcids, profile_keys, openalex_ids),
            )
            for row in cur.fetchall():
                identifiers["profile_keys"].add(_normalize_text(row.get("profile_key")))
                identifiers["orcids"].add(_normalize_orcid(row.get("orcid_id")))
                identifiers["openalex_ids"].add(_normalize_openalex_id(row.get("openalex_id")))
                identifiers["emails"].add(_normalize_email(row.get("email_lower") or row.get("email")))

            cur.execute(
                f"""
                SELECT orcid_id, openalex_id, email
                FROM {self.TABLE_NAME}
                WHERE LOWER(email) = ANY(%s)
                   OR orcid_id = ANY(%s)
                   OR openalex_id = ANY(%s);
                """,
                (emails, orcids, openalex_ids),
            )
            for row in cur.fetchall():
                identifiers["orcids"].add(_normalize_orcid(row.get("orcid_id")))
                identifiers["openalex_ids"].add(_normalize_openalex_id(row.get("openalex_id")))
                identifiers["emails"].add(_normalize_email(row.get("email")))

            cur.execute(
                f"""
                SELECT orcid_id, email
                FROM {self.INVITATION_TABLE_NAME}
                WHERE LOWER(email) = ANY(%s)
                   OR orcid_id = ANY(%s);
                """,
                (emails, orcids),
            )
            for row in cur.fetchall():
                identifiers["orcids"].add(_normalize_orcid(row.get("orcid_id")))
                identifiers["emails"].add(_normalize_email(row.get("email")))

            cur.execute(
                f"""
                SELECT openalex_id, orcid_id, email, all_emails
                FROM {self.HARVESTED_AUTHORS_TABLE}
                WHERE LOWER(email) = ANY(%s)
                   OR orcid_id = ANY(%s)
                   OR openalex_id = ANY(%s)
                   OR LOWER(all_emails) LIKE %s;
                """,
                (
                    emails,
                    orcids,
                    openalex_ids,
                    f"%{normalized_email}%" if normalized_email else "__never_match__",
                ),
            )
            for row in cur.fetchall():
                identifiers["openalex_ids"].add(_normalize_openalex_id(row.get("openalex_id")))
                identifiers["orcids"].add(_normalize_orcid(row.get("orcid_id")))
                identifiers["emails"].add(_normalize_email(row.get("email")))

            cur.execute(
                f"""
                SELECT orcid_id, openalex_id, email
                FROM {self.BULK_EMAIL_RECIPIENTS_TABLE}
                WHERE LOWER(email) = ANY(%s)
                   OR orcid_id = ANY(%s)
                   OR openalex_id = ANY(%s);
                """,
                (emails, orcids, openalex_ids),
            )
            for row in cur.fetchall():
                identifiers["orcids"].add(_normalize_orcid(row.get("orcid_id")))
                identifiers["openalex_ids"].add(_normalize_openalex_id(row.get("openalex_id")))
                identifiers["emails"].add(_normalize_email(row.get("email")))

            for key in list(identifiers):
                identifiers[key] = {value for value in identifiers[key] if value}
            after = sum(len(values) for values in identifiers.values())
            if after == before:
                break
        return identifiers

    def _purge_suppressed_recipient_data(
        self,
        cur,
        identifiers: Dict[str, Set[str]],
        suppressed_email: str,
    ) -> Set[int]:
        """Remove or anonymize mailing-source personal data for a suppressed recipient."""
        emails = sorted(identifiers.get("emails") or {suppressed_email})
        orcids = sorted(identifiers.get("orcids") or [])
        profile_keys = sorted(identifiers.get("profile_keys") or [])
        openalex_ids = sorted(identifiers.get("openalex_ids") or [])
        affected_job_ids: Set[int] = set()

        cur.execute(
            f"""
            DELETE FROM {self.PROFILE_TABLE_NAME}
            WHERE email_lower = ANY(%s)
               OR email = ANY(%s)
               OR orcid_id = ANY(%s)
               OR profile_key = ANY(%s)
               OR openalex_id = ANY(%s);
            """,
            (emails, emails, orcids or [""], profile_keys or [""], openalex_ids or [""]),
        )
        cur.execute(
            f"""
            DELETE FROM {self.HARVESTED_AUTHORS_TABLE}
            WHERE LOWER(email) = ANY(%s)
               OR orcid_id = ANY(%s)
               OR openalex_id = ANY(%s)
               OR LOWER(all_emails) LIKE %s;
            """,
            (
                emails,
                orcids or [""],
                openalex_ids or [""],
                f"%{suppressed_email}%" if suppressed_email else "__never_match__",
            ),
        )
        cur.execute(
            f"""
            DELETE FROM {self.TABLE_NAME}
            WHERE LOWER(email) = ANY(%s)
               OR orcid_id = ANY(%s)
               OR openalex_id = ANY(%s);
            """,
            (emails, orcids or [""], openalex_ids or [""]),
        )
        cur.execute(
            f"""
            UPDATE {self.INVITATION_TABLE_NAME}
            SET orcid_id = 'suppressed:' || id::TEXT,
                author_name = '',
                email = '',
                email_domain = '',
                publisher = ''
            WHERE LOWER(email) = ANY(%s)
               OR orcid_id = ANY(%s);
            """,
            (emails, orcids or [""]),
        )
        cur.execute(
            f"""
            SELECT DISTINCT job_id
            FROM {self.BULK_EMAIL_RECIPIENTS_TABLE}
            WHERE LOWER(email) = ANY(%s)
               OR orcid_id = ANY(%s)
               OR openalex_id = ANY(%s);
            """,
            (emails, orcids or [""], openalex_ids or [""]),
        )
        affected_job_ids = {int(row["job_id"]) for row in cur.fetchall() if row.get("job_id")}
        cur.execute(
            f"""
            UPDATE {self.BULK_EMAIL_RECIPIENTS_TABLE}
            SET status = CASE
                    WHEN status IN (%s, %s) THEN %s
                    ELSE status
                END,
                orcid_id = '',
                author_name = '',
                email = '',
                openalex_id = '',
                specialty = '',
                research_areas = '',
                all_topics_json = '[]',
                recent_publications_json = '[]',
                last_error = CASE
                    WHEN status IN (%s, %s) THEN 'Recipient unsubscribed'
                    ELSE last_error
                END,
                updated_at = NOW()
            WHERE LOWER(email) = ANY(%s)
               OR orcid_id = ANY(%s)
               OR openalex_id = ANY(%s);
            """,
            (
                BULK_RECIPIENT_STATUS_PENDING,
                BULK_RECIPIENT_STATUS_SENDING,
                BULK_RECIPIENT_STATUS_SKIPPED,
                BULK_RECIPIENT_STATUS_PENDING,
                BULK_RECIPIENT_STATUS_SENDING,
                emails,
                orcids or [""],
                openalex_ids or [""],
            ),
        )
        return affected_job_ids

    def suppress_recipient(
        self,
        email: str,
        orcid_id: str = "",
        profile_key: str = "",
        reason: str = "Unsubscribed via email link",
        source: str = EMAIL_SUPPRESSION_SOURCE_UNSUBSCRIBE,
    ) -> Optional[Dict[str, Any]]:
        """Create or update a suppression row for a recipient."""
        if not self.available:
            return None
        normalized_email = _normalize_email(email)
        if not normalized_email:
            return None
        normalized_orcid = _normalize_orcid(orcid_id)
        normalized_profile_key = _normalize_text(profile_key)
        affected_job_ids: Set[int] = set()

        try:
            with self._get_cursor() as cur:
                identifiers = self._collect_suppression_identifiers(
                    cur,
                    normalized_email,
                    orcid_id=normalized_orcid,
                    profile_key=normalized_profile_key,
                )
                cur.execute(
                    f"""
                    SELECT *
                    FROM {self.EMAIL_SUPPRESSIONS_TABLE}
                    WHERE email_lower = %s
                    LIMIT 1;
                    """,
                    (normalized_email,),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        f"""
                        UPDATE {self.EMAIL_SUPPRESSIONS_TABLE}
                        SET is_suppressed = TRUE,
                            suppressed_at = COALESCE(suppressed_at, NOW()),
                            updated_at = NOW(),
                            orcid_id = '',
                            profile_key = '',
                            reason = CASE
                                WHEN %s <> '' THEN %s
                                ELSE reason
                            END,
                            source = CASE
                                WHEN %s <> '' THEN %s
                                ELSE source
                            END
                        WHERE email_lower = %s
                        RETURNING *;
                        """,
                        (
                            reason,
                            reason,
                            source,
                            source,
                            normalized_email,
                        ),
                    )
                    updated = cur.fetchone()
                    result = dict(updated) if updated else dict(row)
                    affected_job_ids = self._purge_suppressed_recipient_data(
                        cur,
                        identifiers,
                        normalized_email,
                    )
                    for job_id in affected_job_ids:
                        self._refresh_bulk_job_counts(job_id)
                    return result

                token = secrets.token_urlsafe(24)
                cur.execute(
                    f"""
                    INSERT INTO {self.EMAIL_SUPPRESSIONS_TABLE}
                        (email_lower, orcid_id, profile_key, unsubscribe_token, is_suppressed, reason, source, suppressed_at, updated_at)
                    VALUES (%s, '', '', %s, TRUE, %s, %s, NOW(), NOW())
                    RETURNING *;
                    """,
                    (
                        normalized_email,
                        token,
                        reason,
                        source,
                    ),
                )
                created = cur.fetchone()
                result = dict(created) if created else None
                affected_job_ids = self._purge_suppressed_recipient_data(
                    cur,
                    identifiers,
                    normalized_email,
                )
                for job_id in affected_job_ids:
                    self._refresh_bulk_job_counts(job_id)
                return result
        except Exception as e:
            print(f"PostgreSQL suppress recipient error: {e}")
            return None

    def register_unsubscribe_token(
        self,
        email: str,
        orcid_id: str = "",
        profile_key: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Ensure a token exists for one recipient without suppressing them."""
        if not self.available:
            return None
        normalized_email = _normalize_email(email)
        if not normalized_email:
            return None
        normalized_orcid = _normalize_orcid(orcid_id)
        normalized_profile_key = _normalize_text(profile_key)

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT *
                    FROM {self.EMAIL_SUPPRESSIONS_TABLE}
                    WHERE email_lower = %s
                    LIMIT 1;
                    """,
                    (normalized_email,),
                )
                existing = cur.fetchone()
                if existing:
                    return dict(existing)

                token = secrets.token_urlsafe(24)
                cur.execute(
                    f"""
                    INSERT INTO {self.EMAIL_SUPPRESSIONS_TABLE}
                        (email_lower, orcid_id, profile_key, unsubscribe_token, is_suppressed, reason, source, suppressed_at, updated_at)
                    VALUES (%s, %s, %s, %s, FALSE, '', %s, NULL, NOW())
                    RETURNING *;
                    """,
                    (
                        normalized_email,
                        normalized_orcid,
                        normalized_profile_key,
                        token,
                        EMAIL_SUPPRESSION_SOURCE_UNSUBSCRIBE,
                    ),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"PostgreSQL register unsubscribe token error: {e}")
            return None

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
        publisher_id = _normalize_text(publisher_id)
        if not publisher_id:
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
            cleaned.append({**recipient, "email": email, "orcid_id": identity_key})

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

    def get_active_bulk_recipient_keys(self, recipients: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
        """Return active queue identities and emails for a recipient collection."""
        result: Dict[str, Set[str]] = {"identities": set(), "emails": set()}
        if not self.available or not recipients:
            return result
        identities = {
            _normalize_orcid(recipient.get("orcid_id"))
            or f"email:{_normalize_email(recipient.get('email'))}"
            for recipient in recipients
            if _normalize_orcid(recipient.get("orcid_id")) or _normalize_email(recipient.get("email"))
        }
        emails = {
            _normalize_email(recipient.get("email"))
            for recipient in recipients
            if _normalize_email(recipient.get("email"))
        }
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT orcid_id, LOWER(email) AS email
                    FROM {self.BULK_EMAIL_RECIPIENTS_TABLE}
                    WHERE status IN (%s, %s)
                      AND (orcid_id = ANY(%s) OR LOWER(email) = ANY(%s));
                    """,
                    (
                        BULK_RECIPIENT_STATUS_PENDING,
                        BULK_RECIPIENT_STATUS_SENDING,
                        sorted(identities) or [""],
                        sorted(emails) or [""],
                    ),
                )
                for row in cur.fetchall():
                    identity = _normalize_text(row.get("orcid_id"))
                    email = _normalize_email(row.get("email"))
                    if identity:
                        result["identities"].add(identity)
                    if email:
                        result["emails"].add(email)
            return result
        except Exception as e:
            print(f"PostgreSQL active bulk recipient lookup error: {e}")
            return result

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
    @staticmethod
    def _collection_config_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
        selected_topic_ids = _decode_json_list(row.get("selected_topic_ids_json"))
        if not selected_topic_ids:
            selected_topic_ids = _decode_json_list(row.get("topic_ids_json"))
        return {
            "domains": _decode_json_list(row.get("domains_json")),
            "disciplines": _decode_json_list(row.get("disciplines_json")),
            "specialties": _decode_json_list(row.get("specialties_json")),
            "exclude_countries": _decode_json_list(row.get("exclude_countries_json")),
            "keyword_tags": row.get("keyword_tags") or "",
            "topic_ids": selected_topic_ids,
            "topic_details": _decode_json_list(row.get("topic_details_json")),
            "h_index_min": row.get("h_index_min"),
            "h_index_max": row.get("h_index_max"),
        }

    @staticmethod
    def _merge_collection_rows(controller: Dict[str, Any], search: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged = dict(controller or {})
        if search:
            merged.update(search)
            merged["search_run_id"] = search.get("id")
            merged["controller_id"] = controller.get("id", 1)
            merged["status"] = controller.get("status") or RUN_STATUS_IDLE
            for field in (
                "baseline_concurrency", "baseline_delay", "effective_concurrency",
                "effective_delay", "last_429_at", "cooldown_until", "stop_until",
                "run_429_count", "clean_batches", "worker_owner", "worker_lease_until",
            ):
                merged[field] = controller.get(field)
        return merged

    def get_or_create_run(self) -> Optional[Dict[str, Any]]:
        """Return the controller merged with its active durable search checkpoint."""
        if not self.available:
            return None
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.COLLECTION_RUNS_TABLE} (id, status)
                    VALUES (1, %s)
                    ON CONFLICT (id) DO NOTHING;
                    """,
                    (RUN_STATUS_IDLE,),
                )
                cur.execute(f"SELECT * FROM {self.COLLECTION_RUNS_TABLE} WHERE id = 1;")
                controller = dict(cur.fetchone() or {})
                search = None
                active_id = controller.get("active_search_run_id")
                if active_id:
                    cur.execute(
                        f"SELECT * FROM {self.COLLECTION_SEARCH_RUNS_TABLE} WHERE id = %s;",
                        (active_id,),
                    )
                    found = cur.fetchone()
                    search = dict(found) if found else None
                if not search:
                    config = self._collection_config_from_row(controller)
                    search_key = build_collection_search_key(config)
                    cur.execute(
                        f"""
                        INSERT INTO {self.COLLECTION_SEARCH_RUNS_TABLE}
                            (search_key, generation, domains_json, disciplines_json,
                             specialties_json, exclude_countries_json, keyword_tags,
                             topic_ids_json, selected_topic_ids_json, topic_details_json, h_index_min, h_index_max,
                             seed_cursor, seed_exhausted)
                        VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, '[]', %s, %s, %s, %s)
                        ON CONFLICT (search_key, generation) DO UPDATE
                        SET search_key = EXCLUDED.search_key
                        RETURNING *;
                        """,
                        (
                            search_key,
                            controller.get("domains_json") or "[]",
                            controller.get("disciplines_json") or "[]",
                            controller.get("specialties_json") or "[]",
                            controller.get("exclude_countries_json") or "[]",
                            controller.get("keyword_tags") or "",
                            controller.get("topic_ids_json") or "[]",
                            controller.get("topic_ids_json") or "[]",
                            controller.get("h_index_min"),
                            controller.get("h_index_max"),
                            controller.get("seed_cursor") or "*",
                            bool(controller.get("seed_exhausted")),
                        ),
                    )
                    search = dict(cur.fetchone())
                    cur.execute(
                        f"UPDATE {self.COLLECTION_RUNS_TABLE} SET active_search_run_id = %s WHERE id = 1;",
                        (search["id"],),
                    )
                    controller["active_search_run_id"] = search["id"]
                return self._merge_collection_rows(controller, search)
        except Exception as e:
            print(f"PostgreSQL get_or_create_run error: {e}")
            return None

    def get_active_run(self) -> Optional[Dict[str, Any]]:
        """Return the current collection run row (alias of get_or_create_run)."""
        return self.get_or_create_run()

    def update_run_state(self, **fields: Any) -> bool:
        """Update controller pacing/status and active-search checkpoint fields."""
        if not self.available or not fields:
            return False
        controller_allowed = {
            "status", "effective_concurrency",
            "effective_delay", "baseline_concurrency", "baseline_delay",
            "last_429_at", "cooldown_until", "stop_until", "run_429_count",
            "clean_batches",
        }
        search_allowed = {"seed_cursor", "seed_exhausted"}
        controller_updates = {k: v for k, v in fields.items() if k in controller_allowed}
        search_updates = {k: v for k, v in fields.items() if k in search_allowed}
        if not controller_updates and not search_updates:
            return False
        try:
            with self._get_cursor() as cur:
                if controller_updates:
                    set_clause = ", ".join(f"{key} = %s" for key in controller_updates)
                    cur.execute(
                        f"UPDATE {self.COLLECTION_RUNS_TABLE} SET {set_clause}, updated_at = NOW() WHERE id = 1;",
                        list(controller_updates.values()),
                    )
                if search_updates:
                    set_clause = ", ".join(f"{key} = %s" for key in search_updates)
                    cur.execute(
                        f"""
                        UPDATE {self.COLLECTION_SEARCH_RUNS_TABLE}
                        SET {set_clause}, updated_at = NOW()
                        WHERE id = (SELECT active_search_run_id FROM {self.COLLECTION_RUNS_TABLE} WHERE id = 1);
                        """,
                        list(search_updates.values()),
                    )
            return True
        except Exception as e:
            print(f"PostgreSQL update_run_state error: {e}")
            return False

    def set_run_status(self, status: str) -> bool:
        """Set the collection run status."""
        return self.update_run_state(status=status)

    def get_search_for_config(self, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the latest generation checkpoint matching all targeting filters."""
        if not self.available:
            return None
        search_key = build_collection_search_key(config)
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT s.*,
                           (SELECT COUNT(*)
                            FROM {self.COLLECTION_RUN_AUTHORS_TABLE} m
                            JOIN {self.HARVESTED_AUTHORS_TABLE} h ON h.openalex_id = m.openalex_id
                            WHERE m.search_run_id = s.id AND h.email_status = %s) AS pending_count
                    FROM {self.COLLECTION_SEARCH_RUNS_TABLE} s
                    WHERE s.search_key = %s
                    ORDER BY s.generation DESC
                    LIMIT 1;
                    """,
                    (EMAIL_STATUS_PENDING, search_key),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        except Exception as e:
            print(f"PostgreSQL get search for config error: {e}")
            return None

    def activate_collection_search(
        self,
        config: Dict[str, Any],
        *,
        topic_details: Optional[List[Dict[str, Any]]] = None,
        start_over: bool = False,
        baseline_concurrency: Optional[int] = None,
        baseline_delay: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically resume an identical checkpoint or create a fresh generation."""
        if not self.available:
            return None
        search_key = build_collection_search_key(config)
        params = _get_connection_params()
        if not params:
            return None
        conn = None
        try:
            conn = psycopg2.connect(**params)
            conn.autocommit = False
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.COLLECTION_RUNS_TABLE} (id, status)
                    VALUES (1, %s) ON CONFLICT (id) DO NOTHING;
                    """,
                    (RUN_STATUS_IDLE,),
                )
                cur.execute(
                    f"""
                    SELECT * FROM {self.COLLECTION_SEARCH_RUNS_TABLE}
                    WHERE search_key = %s
                    ORDER BY generation DESC
                    LIMIT 1
                    FOR UPDATE;
                    """,
                    (search_key,),
                )
                latest = cur.fetchone()
                if latest and not start_over:
                    search = dict(latest)
                else:
                    generation = int(latest.get("generation") or 0) + 1 if latest else 1
                    cur.execute(
                        f"""
                        INSERT INTO {self.COLLECTION_SEARCH_RUNS_TABLE}
                            (search_key, generation, domains_json, disciplines_json,
                             specialties_json, exclude_countries_json, keyword_tags,
                             topic_ids_json, selected_topic_ids_json, topic_details_json,
                             h_index_min, h_index_max)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *;
                        """,
                        (
                            search_key,
                            generation,
                            json.dumps(config.get("domains") or []),
                            json.dumps(config.get("disciplines") or []),
                            json.dumps(config.get("specialties") or []),
                            json.dumps(config.get("exclude_countries") or []),
                            str(config.get("keyword_tags") or "").strip(),
                            json.dumps(config.get("topic_ids") or []),
                            json.dumps(config.get("topic_ids") or []),
                            json.dumps(topic_details or config.get("topic_details") or []),
                            int(config.get("h_index_min") or 0),
                            int(config.get("h_index_max") or 0),
                        ),
                    )
                    search = dict(cur.fetchone())
                concurrency = int(baseline_concurrency or 2)
                delay = float(baseline_delay or 3.0)
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_RUNS_TABLE}
                    SET active_search_run_id = %s,
                        status = %s,
                        baseline_concurrency = %s,
                        baseline_delay = %s,
                        effective_concurrency = %s,
                        effective_delay = %s,
                        cooldown_until = NULL,
                        stop_until = NULL,
                        clean_batches = 0,
                        updated_at = NOW()
                    WHERE id = 1
                    RETURNING *;
                    """,
                    (
                        search["id"], RUN_STATUS_ACTIVE, concurrency, delay,
                        concurrency, delay,
                    ),
                )
                controller = dict(cur.fetchone())
            conn.commit()
            return self._merge_collection_rows(controller, search)
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"PostgreSQL activate collection search error: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def update_current_search_topics(
        self,
        topic_ids: List[str],
        topic_details: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Persist worker-resolved keyword topics without changing search identity."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_SEARCH_RUNS_TABLE}
                    SET topic_ids_json = %s,
                        updated_at = NOW()
                    WHERE id = (SELECT active_search_run_id FROM {self.COLLECTION_RUNS_TABLE} WHERE id = 1);
                    """,
                    (json.dumps(topic_ids or []),),
                )
                return cur.rowcount > 0
        except Exception as e:
            print(f"PostgreSQL update current search topics error: {e}")
            return False

    def acquire_worker_lease(self, owner: str, lease_seconds: int = 120) -> bool:
        """Acquire or renew the singleton collector lease."""
        if not self.available or not owner:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_RUNS_TABLE}
                    SET worker_owner = %s,
                        worker_lease_until = NOW() + (%s * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE id = 1
                      AND (worker_owner = %s OR worker_lease_until IS NULL OR worker_lease_until <= NOW())
                    RETURNING id;
                    """,
                    (owner, max(30, int(lease_seconds)), owner),
                )
                return cur.fetchone() is not None
        except Exception as e:
            print(f"PostgreSQL acquire worker lease error: {e}")
            return False

    def release_worker_lease(self, owner: str) -> bool:
        if not self.available or not owner:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_RUNS_TABLE}
                    SET worker_owner = '', worker_lease_until = NULL, updated_at = NOW()
                    WHERE id = 1 AND worker_owner = %s;
                    """,
                    (owner,),
                )
                return cur.rowcount > 0
        except Exception:
            return False

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

    def persist_seed_batch(
        self,
        search_run_id: int,
        authors: List[Dict[str, Any]],
        *,
        next_cursor: Optional[str],
        has_more: bool,
    ) -> int:
        """Persist a seed page and atomically checkpoint its membership/cursor."""
        if not self.available or not search_run_id:
            return 0
        self.bulk_upsert_harvested_authors(authors, run_id=int(search_run_id))
        openalex_ids = sorted({
            _normalize_openalex_id(author.get("author_id"))
            for author in authors
            if _normalize_openalex_id(author.get("author_id"))
        })
        params = _get_connection_params()
        if not params:
            return 0
        conn = None
        try:
            conn = psycopg2.connect(**params)
            conn.autocommit = False
            with conn.cursor() as cur:
                inserted = 0
                if openalex_ids:
                    cur.execute(
                        f"""
                        INSERT INTO {self.COLLECTION_RUN_AUTHORS_TABLE} (search_run_id, openalex_id)
                        SELECT %s, h.openalex_id
                        FROM {self.HARVESTED_AUTHORS_TABLE} h
                        WHERE h.openalex_id = ANY(%s)
                        ON CONFLICT DO NOTHING;
                        """,
                        (int(search_run_id), openalex_ids),
                    )
                    inserted = int(cur.rowcount or 0)
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_SEARCH_RUNS_TABLE}
                    SET seed_cursor = %s,
                        seed_exhausted = %s,
                        seeded = seeded + %s,
                        updated_at = NOW()
                    WHERE id = %s;
                    """,
                    (next_cursor or "", not bool(has_more and next_cursor), inserted, int(search_run_id)),
                )
            conn.commit()
            return inserted
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"PostgreSQL persist seed batch error: {e}")
            return 0
        finally:
            if conn:
                conn.close()

    def get_pending_harvest(
        self, limit: int = 50, require_orcid: bool = True,
        search_run_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return pending authors belonging to the active search only."""
        if not self.available:
            return []
        safe_limit = max(1, min(int(limit or 50), 1000))
        orcid_clause = "AND orcid_id <> ''" if require_orcid else ""
        try:
            with self._get_cursor() as cur:
                if search_run_id:
                    cur.execute(
                        f"""
                        SELECT h.* FROM {self.COLLECTION_RUN_AUTHORS_TABLE} m
                        JOIN {self.HARVESTED_AUTHORS_TABLE} h ON h.openalex_id = m.openalex_id
                        WHERE m.search_run_id = %s
                          AND h.email_status = %s {orcid_clause}
                          AND (h.next_retry_at IS NULL OR h.next_retry_at <= NOW())
                        ORDER BY h.next_retry_at ASC NULLS FIRST, h.created_at ASC
                        LIMIT %s;
                        """,
                        (int(search_run_id), EMAIL_STATUS_PENDING, safe_limit),
                    )
                else:
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

    def count_search_harvest_by_status(self, search_run_id: int) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        if not self.available or not search_run_id:
            return counts
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    SELECT h.email_status, COUNT(*) AS cnt
                    FROM {self.COLLECTION_RUN_AUTHORS_TABLE} m
                    JOIN {self.HARVESTED_AUTHORS_TABLE} h ON h.openalex_id = m.openalex_id
                    WHERE m.search_run_id = %s
                    GROUP BY h.email_status;
                    """,
                    (int(search_run_id),),
                )
                for row in cur.fetchall():
                    counts[row["email_status"]] = int(row["cnt"])
            return counts
        except Exception as e:
            print(f"PostgreSQL count search harvest error: {e}")
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

    def bump_search_stat(self, search_run_id: int, field: str, increment: int = 1) -> bool:
        """Increment a metric owned by one durable search generation."""
        if not self.available or not search_run_id:
            return False
        allowed = {"emails_found", "attempts", "orcid_429", "openalex_429", "seeded"}
        if field not in allowed:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {self.COLLECTION_SEARCH_RUNS_TABLE}
                    SET {field} = {field} + %s, updated_at = NOW()
                    WHERE id = %s;
                    """,
                    (int(increment), int(search_run_id)),
                )
                return cur.rowcount > 0
        except Exception as e:
            print(f"PostgreSQL bump search stat error: {e}")
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
            "search_available": 0,
            "search_seeded": 0,
        }
        if not self.available:
            return summary
        try:
            summary["run"] = self.get_or_create_run()
            global_counts = self.count_harvest_by_status()
            summary["total_collected"] = global_counts.get(EMAIL_STATUS_FOUND, 0)
            search_run_id = (summary["run"] or {}).get("search_run_id")
            with self._get_cursor() as cur:
                if search_run_id:
                    cur.execute(
                        f"""
                        SELECT h.email_status, COUNT(*) AS cnt
                        FROM {self.COLLECTION_RUN_AUTHORS_TABLE} m
                        JOIN {self.HARVESTED_AUTHORS_TABLE} h ON h.openalex_id = m.openalex_id
                        WHERE m.search_run_id = %s
                        GROUP BY h.email_status;
                        """,
                        (int(search_run_id),),
                    )
                    counts = {row["email_status"]: int(row["cnt"]) for row in cur.fetchall()}
                else:
                    counts = {}
            summary["status_counts"] = counts
            summary["queue_pending"] = counts.get(EMAIL_STATUS_PENDING, 0)
            summary["search_available"] = counts.get(EMAIL_STATUS_FOUND, 0)
            run = summary["run"] or {}
            summary["search_seeded"] = int(run.get("seeded") or 0)
            summary["emails_found_today"] = int(run.get("emails_found") or 0)
            summary["attempts_today"] = int(run.get("attempts") or 0)
            summary["orcid_429_today"] = int(run.get("orcid_429") or 0)
            if run:
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
