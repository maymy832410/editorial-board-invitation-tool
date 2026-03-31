-- Table schema for Railway PostgreSQL (auto-created by the app on startup).
-- You do NOT need to run this manually; the app creates the table automatically.

CREATE TABLE IF NOT EXISTS sent_invitations (
  orcid_id    TEXT PRIMARY KEY,
  author_name TEXT DEFAULT '',
  email       TEXT DEFAULT '',
  publisher   TEXT DEFAULT '',
  sent_at     TIMESTAMPTZ DEFAULT NOW()
);
