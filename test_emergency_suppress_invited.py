import unittest

from emergency_suppress_invited import (
    CONFIRMATION_TEXT,
    collect_invited_emails,
    normalize_email,
    run,
    suppress_inventory,
)


class FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _query):
        return None

    def fetchall(self):
        if not self.responses:
            return []
        return self.responses.pop(0)

    def fetchone(self):
        if not self.responses:
            return {"cnt": 0}
        response = self.responses.pop(0)
        if isinstance(response, list):
            return response[0] if response else {"cnt": 0}
        return response


class FakeStorage:
    TABLE_NAME = "sent_invitations"
    INVITATION_TABLE_NAME = "author_invitations"
    PROFILE_TABLE_NAME = "author_profiles"
    BULK_EMAIL_RECIPIENTS_TABLE = "bulk_email_recipients"
    EMAIL_SUPPRESSIONS_TABLE = "email_suppressions"

    def __init__(self, responses=None, suppressed=None):
        self.available = True
        self.error_message = ""
        self.responses = responses or []
        self.suppressed = set(suppressed or [])
        self.suppressed_calls = []

    def _get_cursor(self):
        return FakeCursor(self.responses)

    def is_email_suppressed(self, email):
        return email in self.suppressed

    def suppress_recipient(self, email, reason="", source=""):
        self.suppressed_calls.append((email, reason, source))
        self.suppressed.add(email)
        return {"email_lower": email, "is_suppressed": True}


class EmergencySuppressInvitedTests(unittest.TestCase):
    def test_normalize_email_rejects_blank_and_invalid(self):
        self.assertEqual(normalize_email(" Person@Example.COM "), "person@example.com")
        self.assertEqual(normalize_email("not-an-email"), "")
        self.assertEqual(normalize_email(""), "")

    def test_collect_invited_emails_dedupes_case_insensitively(self):
        storage = FakeStorage(responses=[
            [{"email": "Person@Example.com"}, {"email": ""}],
            [{"email": "person@example.com"}],
            [{"email": "bulk@example.com", "job_id": 42, "status": "pending"}],
            [{"email": "PROFILE@example.com"}],
        ])
        inventory = collect_invited_emails(storage)

        self.assertEqual(
            inventory.emails,
            {"person@example.com", "bulk@example.com", "profile@example.com"},
        )
        self.assertEqual(inventory.bulk_job_ids, {42})
        self.assertEqual(inventory.bulk_pending_or_sending, 1)

    def test_run_without_confirmation_is_dry_run_and_does_not_suppress(self):
        storage = FakeStorage(responses=[
            [{"email": "person@example.com"}],
            [],
            [],
            [],
        ])

        exit_code = run(["--dry-run"], storage=storage)

        self.assertEqual(exit_code, 0)
        self.assertEqual(storage.suppressed_calls, [])

    def test_suppress_inventory_continues_and_counts_existing(self):
        storage = FakeStorage(suppressed={"already@example.com"})
        inventory = collect_invited_emails(FakeStorage(responses=[
            [{"email": "already@example.com"}, {"email": "new@example.com"}],
            [],
            [],
            [],
        ]))

        result = suppress_inventory(storage, inventory)

        self.assertEqual(result["already_suppressed"], 1)
        self.assertEqual(result["newly_suppressed"], 1)
        self.assertEqual(result["failed_count"], 0)

    def test_confirm_constant_is_exact(self):
        self.assertEqual(CONFIRMATION_TEXT, "SUPPRESS_ALL_INVITED")


if __name__ == "__main__":
    unittest.main()
