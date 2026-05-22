"""PostgreSQL client for persistent storage of sent invitations (Railway)."""

import os
from datetime import datetime, timezone
from typing import Optional, Set, Dict, List
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras


INVITATION_TYPE_EDITORIAL = "editorial"
INVITATION_TYPE_PUBLICATION = "publication"


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
                CREATE TABLE IF NOT EXISTS {self.INVITATION_TABLE_NAME} (
                    id              SERIAL PRIMARY KEY,
                    orcid_id        TEXT NOT NULL,
                    invitation_type TEXT NOT NULL DEFAULT '{INVITATION_TYPE_EDITORIAL}',
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
            cur.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_author_invitations_unique
                ON {self.INVITATION_TABLE_NAME} (orcid_id, invitation_type, journal_name);
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_author_invitations_type
                ON {self.INVITATION_TABLE_NAME} (invitation_type);
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
    ) -> bool:
        """Upsert the invitation-type-aware send log."""
        if not self.available or not orcid_id:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {self.INVITATION_TABLE_NAME}
                        (orcid_id, invitation_type, author_name, email, publisher, journal_name,
                         template_id, cite_score, quartile, sent_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (orcid_id, invitation_type, journal_name) DO UPDATE SET
                        author_name = EXCLUDED.author_name,
                        email       = EXCLUDED.email,
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
                    publisher,
                    journal_name,
                    template_id,
                    cite_score,
                    quartile,
                    datetime.now(timezone.utc),
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
        }

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
        )

        if invitation_type != INVITATION_TYPE_EDITORIAL:
            return typed_ok

        try:
            with self._get_cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {self.TABLE_NAME} (orcid_id, author_name, email, publisher, sent_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (orcid_id) DO UPDATE SET
                        author_name = EXCLUDED.author_name,
                        email       = EXCLUDED.email,
                        publisher   = EXCLUDED.publisher,
                        sent_at     = EXCLUDED.sent_at;
                """, (orcid_id, author_name, email, publisher, datetime.now(timezone.utc)))
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
