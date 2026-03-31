"""PostgreSQL client for persistent storage of sent invitations (Railway)."""

import os
from datetime import datetime, timezone
from typing import Optional, Set, Dict, List
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras


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
        "sslmode": "require",
    }


class PostgresStorage:
    """Persistent storage using PostgreSQL for sent invitations."""

    TABLE_NAME = "sent_invitations"

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

    def get_status(self) -> Dict:
        """Get database status for UI display."""
        return {
            "available": self.available,
            "error": self.error_message,
            "table": self.TABLE_NAME,
        }

    def mark_sent(self, orcid_id: str, author_name: str = "", email: str = "", publisher: str = "") -> bool:
        """Mark an author as sent invitation (upsert)."""
        if not self.available:
            return False
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
            return True
        except Exception as e:
            print(f"PostgreSQL mark_sent error: {e}")
            return False

    def is_sent(self, orcid_id: str) -> bool:
        """Check if author has been sent invitation."""
        if not self.available:
            return False
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    f"SELECT 1 FROM {self.TABLE_NAME} WHERE orcid_id = %s LIMIT 1;",
                    (orcid_id,),
                )
                return cur.fetchone() is not None
        except Exception as e:
            print(f"PostgreSQL is_sent error: {e}")
            return False

    def get_all_sent(self) -> Set[str]:
        """Get all sent ORCID IDs."""
        if not self.available:
            return set()
        try:
            with self._get_cursor() as cur:
                cur.execute(f"SELECT orcid_id FROM {self.TABLE_NAME};")
                return {row["orcid_id"] for row in cur.fetchall()}
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
