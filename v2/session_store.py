"""Per-user session management for the FastAPI app.

Sessions are cookie-based with server-side state stored in the database.
Each session isolates: search results, filters, pagination state, selected authors.
"""

import json
import secrets
import time
from typing import Any, Optional

import psycopg2
import psycopg2.extras

SESSION_COOKIE_NAME = "eb_session_id"
SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours


def _get_connection_params() -> dict:
    """Get database connection params from DATABASE_URL env var."""
    import os
    from urllib.parse import urlparse
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return {}
    parsed = urlparse(database_url)
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "dbname": parsed.path.lstrip("/"),
        "user": parsed.username,
        "password": parsed.password,
    }


def _get_conn():
    """Get a psycopg2 connection."""
    params = _get_connection_params()
    if not params:
        return None
    conn = psycopg2.connect(**params)
    conn.autocommit = True
    return conn


def ensure_session_table():
    """Create the user_sessions table if it doesn't exist."""
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    data JSONB DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                    ON user_sessions(updated_at);
            """)
    finally:
        conn.close()


def create_session() -> str:
    """Create a new session and return its ID."""
    conn = _get_conn()
    if not conn:
        return secrets.token_urlsafe(32)
    try:
        session_id = secrets.token_urlsafe(32)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_sessions (session_id, data) VALUES (%s, %s)",
                (session_id, json.dumps({})),
            )
        return session_id
    finally:
        conn.close()


def get_session_data(session_id: str) -> Optional[dict]:
    """Get session data for a given session ID."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT data FROM user_sessions WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if row:
                # Update the updated_at timestamp
                cur.execute(
                    "UPDATE user_sessions SET updated_at = NOW() WHERE session_id = %s",
                    (session_id,),
                )
                return dict(row["data"]) if row["data"] else {}
        return None
    finally:
        conn.close()


def update_session(session_id: str, data: dict) -> bool:
    """Update session data."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_sessions SET data = %s::jsonb, updated_at = NOW() WHERE session_id = %s",
                (json.dumps(data), session_id),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def delete_expired_sessions():
    """Delete sessions older than SESSION_TTL_SECONDS."""
    conn = _get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_sessions WHERE updated_at < NOW() - INTERVAL '%s seconds'",
                (SESSION_TTL_SECONDS,),
            )
    finally:
        conn.close()


class SessionData:
    """Convenience wrapper for accessing and mutating session data."""

    def __init__(self, session_id: str, data: Optional[dict] = None):
        self.session_id = session_id
        self._data = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._data[key] = value

    def delete(self, key: str):
        self._data.pop(key, None)

    def to_dict(self) -> dict:
        return dict(self._data)

    def save(self) -> bool:
        return update_session(self.session_id, self._data)

    @property
    def search_results(self) -> list:
        return self._data.get("search_results", [])

    @search_results.setter
    def search_results(self, value: list):
        self._data["search_results"] = value

    @property
    def search_params(self) -> dict:
        return self._data.get("search_params", {
            "h_index_min": 10,
            "h_index_max": 50,
            "countries": [],
            "disciplines": [],
            "author_source_mode": "both",
            "max_results": 500,
            "jump_size": 250,
        })

    @search_params.setter
    def search_params(self, value: dict):
        self._data["search_params"] = value

    @property
    def journal_config(self) -> dict:
        return self._data.get("journal_config", {
            "name": "",
            "issn": "",
            "link": "",
            "location": "",
            "editor_in_chief": "",
            "submission_link": "",
            "cite_score": "",
            "quartile": "",
            "indexing_status": "",
            "invitation_goal": "Regular submission",
            "scope": "",
        })

    @journal_config.setter
    def journal_config(self, value: dict):
        self._data["journal_config"] = value

    @property
    def publisher(self) -> str:
        return self._data.get("publisher", "brevo")

    @publisher.setter
    def publisher(self, value: str):
        self._data["publisher"] = value

    @property
    def search_checkpoints(self) -> dict:
        return self._data.get("search_checkpoints", {})

    @search_checkpoints.setter
    def search_checkpoints(self, value: dict):
        self._data["search_checkpoints"] = value

    @property
    def search_batch_cache(self) -> dict:
        return self._data.get("search_batch_cache", {})

    @search_batch_cache.setter
    def search_batch_cache(self, value: dict):
        self._data["search_batch_cache"] = value

    @property
    def processed_orcids(self) -> set:
        raw = self._data.get("processed_orcids", [])
        if isinstance(raw, list):
            return set(raw)
        return set()

    @processed_orcids.setter
    def processed_orcids(self, value: set):
        self._data["processed_orcids"] = list(value)

    @property
    def recent_publications_cache(self) -> dict:
        """Cache of recent publications per author ORCID."""
        return self._data.get("recent_publications_cache", {})

    @recent_publications_cache.setter
    def recent_publications_cache(self, value: dict):
        self._data["recent_publications_cache"] = value
