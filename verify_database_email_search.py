"""Non-production checks for database recipient search wiring."""

from pathlib import Path

from author_filters import dedupe_authors


ROOT = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_database_query_contract() -> None:
    source = _read("db_client.py")
    method_start = source.index("def search_database_email_recipients")
    method_end = source.index("def get_invitation_counts", method_start)
    method = source[method_start:method_end]

    _assert("PROFILE_TABLE_NAME" in method, "database search must include author_profiles")
    _assert("HARVESTED_AUTHORS_TABLE" in method, "database search must include harvested_authors")
    _assert("institution" in method, "database search must include affiliation/institution")
    _assert("EMAIL_SUPPRESSIONS_TABLE" in method, "database search must respect suppressions")
    _assert("LOWER(COALESCE" in method, "database search must support text matching across fields")
    _assert("requested_limit = max(0" in method, "database search should allow uncapped results with limit=0")
    _assert("min(int(limit or 500), 5000)" not in method, "database search must not enforce the old 5000-row cap")


def check_app_wiring_contract() -> None:
    source = _read("app.py")
    _assert("render_database_email_search_panel" in source, "Author Invitation should render a dedicated database search panel")
    _assert("Database Email Search" in source, "UI should expose database email search with a visible heading")
    _assert("Database email search is uncapped" in source, "database search UI should explain that results are uncapped")
    _assert("database_email_search_limit" not in source, "database search UI should not expose the old 5000-row limit")
    _assert('"Search within results"' in source, "shared text search should be visible in invitation tabs")
    _assert("empty_results_message" in source, "empty result messaging should not return before filters render")
    _assert("Load or search authors to populate country filter options." in source, "country filter should render empty state")
    _assert("Load or search authors to populate author domain filter options." in source, "domain filters should render empty state")
    _assert("_search_database_email_rows" in source, "UI should use mapped database search rows")
    _assert("filters['database_email_panel']" in source, "database search results should feed the author result table")
    _assert("_recipient_tracking_id" in source, "email-only rows need a stable sent-tracking identity")
    _assert("email_dialog(pending_author, dialog_filters)" in source, "database rows should reuse preview dialog")
    _assert("is_suppressed=lambda email, orcid_id" in source, "bulk queue should skip suppressed rows")
    _assert('"Suppressed"' in source, "suppressed visible rows should be disabled")


def check_bulk_tracking_contract() -> None:
    helper = _read("bulk_email_jobs.py")
    storage = _read("db_client.py")

    _assert('tracking_id = orcid_id or f"email:{normalized_email}"' in helper, "bulk prep should check sent status for email-only rows")
    _assert('identity_key = orcid_id or f"email:{email}"' in storage, "bulk storage should preserve email-only tracking identity")
    _assert('"orcid_id": identity_key' in storage, "bulk worker needs the tracking identity to mark email-only sends")


def check_dedupe_for_email_only_records() -> None:
    rows = [
        {"name": "First", "email": "person@example.com", "orcid_id": ""},
        {"name": "Duplicate", "email": "person@example.com", "orcid_id": ""},
    ]
    deduped = dedupe_authors(rows)
    _assert(len(deduped) == 1, "email-only database records should dedupe by email")


def main() -> None:
    check_database_query_contract()
    check_app_wiring_contract()
    check_bulk_tracking_contract()
    check_dedupe_for_email_only_records()
    print("Database email search verification passed.")


if __name__ == "__main__":
    main()
