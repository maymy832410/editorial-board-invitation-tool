import unittest

from bulk_email_worker import BulkEmailWorker


class FakeStorage:
    def __init__(self):
        self.calls = []

    def suppress_recipient(self, **kwargs):
        self.calls.append(kwargs)
        return {"is_suppressed": True}


class BulkEmailWorkerCleanupTests(unittest.TestCase):
    def make_worker(self):
        worker = BulkEmailWorker.__new__(BulkEmailWorker)
        worker.storage = FakeStorage()
        return worker

    def test_cleanup_is_opt_in(self):
        worker = self.make_worker()

        worker._suppress_after_success(
            {"journal_config_json": "{}"},
            {"id": 1, "email": "person@example.com", "orcid_id": "0000-0001"},
        )

        self.assertEqual(worker.storage.calls, [])

    def test_cleanup_suppresses_delivered_recipient_for_v1_job(self):
        worker = self.make_worker()

        worker._suppress_after_success(
            {"journal_config_json": '{"suppress_after_send": true}'},
            {
                "id": 2,
                "email": "person@example.com",
                "orcid_id": "0000-0001",
                "profile_key": "profile-1",
            },
        )

        self.assertEqual(len(worker.storage.calls), 1)
        self.assertEqual(worker.storage.calls[0]["email"], "person@example.com")
        self.assertEqual(worker.storage.calls[0]["source"], "automatic_post_send")


if __name__ == "__main__":
    unittest.main()
