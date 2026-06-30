import os
import unittest
from unittest.mock import patch

from db_client import (
    EMAIL_STATUS_FOUND,
    PostgresStorage,
    build_collection_search_key,
)
from openalex_client import OpenAlexClient


class CollectionIdentityTests(unittest.TestCase):
    def test_fingerprint_is_order_and_case_insensitive(self):
        first = {
            "disciplines": ["Medicine", "Psychology"],
            "specialties": ["Cancer Biology"],
            "exclude_countries": ["us", "GB"],
            "keyword_tags": "Genomics, machine learning",
            "topic_ids": ["https://openalex.org/T123", "T456"],
            "h_index_min": 3,
            "h_index_max": 50,
        }
        reordered = {
            "disciplines": ["psychology", "medicine"],
            "specialties": ["cancer biology"],
            "exclude_countries": ["gb", "US"],
            "keyword_tags": "MACHINE LEARNING, genomics",
            "topic_ids": ["t456", "T123"],
            "h_index_min": 3,
            "h_index_max": 50,
        }
        self.assertEqual(
            build_collection_search_key(first),
            build_collection_search_key(reordered),
        )

    def test_any_targeting_change_changes_fingerprint(self):
        base = {"disciplines": ["Medicine"], "h_index_min": 3, "h_index_max": 50}
        changed = dict(base, h_index_max=51)
        self.assertNotEqual(
            build_collection_search_key(base),
            build_collection_search_key(changed),
        )


class TopicSuggestionTests(unittest.TestCase):
    def test_suggestions_preserve_topic_identity_and_context(self):
        payload = {
            "results": [{
                "id": "https://openalex.org/T10158",
                "display_name": "Cancer Immunotherapy and Biomarkers",
                "subfield": {"display_name": "Oncology"},
                "field": {"display_name": "Medicine"},
                "works_count": 12345,
            }]
        }
        client = OpenAlexClient()
        with patch.object(client, "_make_request", return_value=payload):
            result = client.search_topic_suggestions("cancer")
        self.assertEqual(result[0]["id"], "T10158")
        self.assertEqual(result[0]["field"], "Medicine")
        self.assertEqual(result[0]["works_count"], 12345)


@unittest.skipUnless(
    os.environ.get("COLLECTION_TEST_DATABASE_URL"),
    "COLLECTION_TEST_DATABASE_URL is required for PostgreSQL integration tests",
)
class CollectionDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATABASE_URL"] = os.environ["COLLECTION_TEST_DATABASE_URL"]
        cls.storage = PostgresStorage()
        if not cls.storage.available:
            raise RuntimeError(cls.storage.error_message)

    def setUp(self):
        with self.storage._get_cursor() as cur:
            cur.execute("TRUNCATE collection_run_authors, collection_search_runs RESTART IDENTITY CASCADE;")
            cur.execute("TRUNCATE harvested_authors CASCADE;")
            cur.execute("DELETE FROM collection_runs;")
            cur.execute("DELETE FROM collection_daily_stats;")
            cur.execute("DELETE FROM email_suppressions;")
        self.storage.get_or_create_run()

    @staticmethod
    def config():
        return {
            "disciplines": ["Medicine"],
            "specialties": ["Cancer"],
            "exclude_countries": ["US"],
            "keyword_tags": "genomics",
            "topic_ids": ["T10158"],
            "h_index_min": 3,
            "h_index_max": 50,
        }

    def test_resume_and_start_over(self):
        first = self.storage.activate_collection_search(self.config())
        self.storage.update_run_state(seed_cursor="resume-token")
        self.storage.bump_search_stat(first["search_run_id"], "attempts", 7)

        resumed = self.storage.activate_collection_search(self.config())
        self.assertEqual(resumed["search_run_id"], first["search_run_id"])
        self.assertEqual(resumed["seed_cursor"], "resume-token")
        self.assertEqual(resumed["attempts"], 7)

        restarted = self.storage.activate_collection_search(self.config(), start_over=True)
        self.assertNotEqual(restarted["search_run_id"], first["search_run_id"])
        self.assertEqual(restarted["generation"], 2)
        self.assertEqual(restarted["seed_cursor"], "*")
        self.assertEqual(restarted["attempts"], 0)

    def test_membership_scopes_queue_and_reuses_found_email(self):
        first = self.storage.activate_collection_search(self.config())
        author = {
            "author_id": "https://openalex.org/A1",
            "orcid_id": "0000-0001",
            "name": "Author One",
            "discipline": "Medicine",
            "specialty": "Cancer",
            "all_topics": ["Cancer"],
        }
        self.storage.persist_seed_batch(
            first["search_run_id"], [author], next_cursor="next", has_more=True
        )
        self.assertEqual(
            len(self.storage.get_pending_harvest(search_run_id=first["search_run_id"])), 1
        )
        self.storage.update_harvest_email(
            author["author_id"], email="author@example.com", status=EMAIL_STATUS_FOUND
        )

        restarted = self.storage.activate_collection_search(self.config(), start_over=True)
        self.storage.persist_seed_batch(
            restarted["search_run_id"], [author], next_cursor="next-2", has_more=True
        )
        self.assertEqual(
            self.storage.get_pending_harvest(search_run_id=restarted["search_run_id"]), []
        )
        self.assertEqual(
            self.storage.count_search_harvest_by_status(restarted["search_run_id"]),
            {"found": 1},
        )

    def test_worker_lease_has_one_owner(self):
        self.assertTrue(self.storage.acquire_worker_lease("worker-a", 60))
        self.assertFalse(self.storage.acquire_worker_lease("worker-b", 60))
        self.assertTrue(self.storage.acquire_worker_lease("worker-a", 60))
        self.assertTrue(self.storage.release_worker_lease("worker-a"))
        self.assertTrue(self.storage.acquire_worker_lease("worker-b", 60))

    def test_bulk_suppression_lookup_resolves_all_identifier_types(self):
        with self.storage._get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_suppressions
                    (email_lower, orcid_id, profile_key, unsubscribe_token, is_suppressed)
                VALUES (%s, %s, %s, %s, TRUE);
                """,
                ("blocked@example.com", "0000-0001", "orcid:0000-0001", "test-token"),
            )
        keys = self.storage.get_suppressed_recipient_keys([
            {
                "email": "BLOCKED@example.com",
                "orcid_id": "https://orcid.org/0000-0001",
                "profile_key": "orcid:0000-0001",
            },
            {"email": "allowed@example.com", "orcid_id": "0000-0002"},
        ])
        self.assertEqual(keys["emails"], {"blocked@example.com"})
        self.assertEqual(keys["orcids"], {"0000-0001"})
        self.assertEqual(keys["profile_keys"], {"orcid:0000-0001"})


if __name__ == "__main__":
    unittest.main()
