"""Non-production checks for the GDPR unsubscribe flow.

This script avoids live database or SMTP access. It validates the key source
contracts that protect the unsubscribe behavior from accidental regressions.
"""

from pathlib import Path

from bulk_email_jobs import prepare_bulk_recipients


ROOT = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_bulk_suppression_filter() -> None:
    authors = [
        {"name": "Allowed", "orcid_id": "0000-0001", "email": "allowed@example.com"},
        {"name": "Blocked", "orcid_id": "0000-0002", "email": "blocked@example.com"},
    ]
    recipients = prepare_bulk_recipients(
        authors,
        is_already_sent=lambda _orcid: False,
        is_suppressed=lambda email, _orcid: email.lower() == "blocked@example.com",
    )
    _assert(len(recipients) == 1, "suppressed recipients must be skipped before queueing")
    _assert(recipients[0]["email"] == "allowed@example.com", "allowed recipient should remain")


def check_email_headers() -> None:
    source = _read("email_sender.py")
    _assert("List-Unsubscribe" in source, "standard List-Unsubscribe header should be present")
    _assert(
        "List-Unsubscribe-Post" not in source,
        "one-click POST header must not be advertised without a POST endpoint",
    )
    _assert(
        "You may opt out of future invitations at any time." in source,
        "footer should clearly state the opt-out right",
    )


def check_db_contract() -> None:
    source = _read("db_client.py")
    register_start = source.index("def register_unsubscribe_token")
    register_block = source[register_start: source.index("# ------------------------------------------------------------------", register_start)]
    _assert("FALSE" in register_block, "token registration must not suppress by itself")
    _assert(
        "_purge_suppressed_recipient_data" in source,
        "suppression should purge or anonymize mailing-source personal data",
    )
    _assert(
        "orcid_id = ''" in source and "profile_key = ''" in source,
        "suppression tombstone should clear non-minimal identifiers",
    )


def check_app_contract() -> None:
    source = _read("app.py")
    invalid_token_branch = source[source.index("if not record:"): source.index("updated = db_storage.suppress_recipient")]
    _assert("st.stop()" in invalid_token_branch, "invalid tokens must stop before mutation")
    _assert("Manual Unsubscribe" in source, "admin manual unsubscribe tool should exist")
    _assert('source="manual_admin"' in source, "manual unsubscribe should use the shared suppression path")


def main() -> None:
    check_bulk_suppression_filter()
    check_email_headers()
    check_db_contract()
    check_app_contract()
    print("Unsubscribe flow verification passed.")


if __name__ == "__main__":
    main()
