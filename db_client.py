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


# Singleton instance
_storage: Optional[PostgresStorage] = None


def get_storage() -> PostgresStorage:
    """Get or create PostgreSQL storage singleton."""
    global _storage
    if _storage is None:
        _storage = PostgresStorage()
    return _storage
