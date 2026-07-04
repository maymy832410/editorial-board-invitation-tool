import unittest

from db_client import PostgresStorage


class FakeStorage:
    available = True
    search_database_email_recipients_page = PostgresStorage.search_database_email_recipients_page

    def __init__(self, rows_by_source):
        self.rows_by_source = rows_by_source
        self.calls = []

    def search_database_email_recipients(self, source="all", limit=0, **kwargs):
        self.calls.append({"source": source, "limit": limit, **kwargs})
        return list(self.rows_by_source.get(source, []))[:limit]


class DatabaseEmailPaginationTests(unittest.TestCase):
    def test_combined_sources_are_sorted_deduped_and_paged(self):
        storage = FakeStorage({
            "profiles": [
                {"orcid_id": "1", "email": "one@example.com", "updated_at": "2026-03-01"},
                {"orcid_id": "2", "email": "two@example.com", "updated_at": "2026-01-01"},
            ],
            "harvested": [
                {"orcid_id": "1", "email": "one@example.com", "updated_at": "2026-02-01"},
                {"orcid_id": "3", "email": "three@example.com", "updated_at": "2026-02-15"},
            ],
        })

        first = storage.search_database_email_recipients_page(page_size=2)
        second = storage.search_database_email_recipients_page(page_size=2, cursor=first["next_cursor"])

        self.assertEqual([row["orcid_id"] for row in first["rows"]], ["1", "3"])
        self.assertTrue(first["has_next"])
        self.assertEqual([row["orcid_id"] for row in second["rows"]], ["2"])
        self.assertEqual(second["previous_cursor"], "0")

    def test_page_queries_are_bounded_to_cursor_plus_page_and_lookahead(self):
        storage = FakeStorage({"profiles": [], "harvested": []})

        storage.search_database_email_recipients_page(page_size=100, cursor="200")

        self.assertEqual([call["limit"] for call in storage.calls], [301, 301])

    def test_country_filters_are_forwarded_to_each_source(self):
        storage = FakeStorage({"profiles": [], "harvested": []})
        storage.search_database_email_recipients_page(
            countries=["US", "GB"], exclude_countries=["DE"], page_size=100
        )
        self.assertTrue(storage.calls)
        for call in storage.calls:
            self.assertEqual(call["countries"], ["US", "GB"])
            self.assertEqual(call["exclude_countries"], ["DE"])


if __name__ == "__main__":
    unittest.main()
