import unittest

from bulk_email_jobs import MAX_BULK_RECIPIENTS, cap_bulk_recipients, prepare_bulk_recipients


class BulkEmailJobTests(unittest.TestCase):
    def test_bulk_recipient_cap_is_1000(self):
        authors = [{"orcid_id": str(index)} for index in range(1200)]

        capped = cap_bulk_recipients(authors)

        self.assertEqual(MAX_BULK_RECIPIENTS, 1000)
        self.assertEqual(len(capped), 1000)
        self.assertEqual(capped[-1]["orcid_id"], "999")

    def test_prepare_bulk_recipients_skips_invalid_duplicate_sent_and_retracted(self):
        authors = [
            {"name": "Ready One", "orcid_id": "0000-0001", "email": "one@example.com"},
            {"name": "Duplicate One", "orcid_id": "0000-0001", "email": "dupe@example.com"},
            {"name": "Already Sent", "orcid_id": "0000-0002", "email": "sent@example.com"},
            {"name": "No Email", "orcid_id": "0000-0003", "email": ""},
            {"name": "Retracted Author", "orcid_id": "0000-0004", "email": "bad@example.com"},
            {"name": "Email Only", "orcid_id": "", "email": "email@example.com"},
            {"name": "Email Only Again", "orcid_id": "", "email": "EMAIL@example.com"},
        ]

        recipients = prepare_bulk_recipients(
            authors,
            is_already_sent=lambda orcid_id: orcid_id == "0000-0002",
            retracted_names={"retracted author"},
        )

        self.assertEqual(
            [recipient["name"] for recipient in recipients],
            ["Ready One", "Email Only"],
        )


if __name__ == "__main__":
    unittest.main()
