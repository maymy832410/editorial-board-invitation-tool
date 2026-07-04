import unittest
from unittest.mock import patch

from openalex_client import OpenAlexClient


class OpenAlexCountryFilterTests(unittest.TestCase):
    def setUp(self):
        self.client = OpenAlexClient()

    def test_multiple_included_countries_use_or(self):
        value = self.client.build_filter(include_country_codes=["US", "GB"])
        self.assertIn("last_known_institutions.country_code:GB|US", value)

    def test_include_wins_over_exclude(self):
        value = self.client.build_filter(
            include_country_codes=["US"], exclude_country_codes=["US", "DE"]
        )
        self.assertIn("country_code:US", value)
        self.assertIn("country_code:!DE", value)
        self.assertNotIn("!US", value)

    def test_no_country_filter_adds_no_country_clause(self):
        value = self.client.build_filter()
        self.assertNotIn("country_code", value)

    def test_batch_of_250_uses_two_openalex_pages(self):
        first = {"results": [{}] * 200, "meta": {"next_cursor": "next"}}
        second = {"results": [{}] * 50, "meta": {"next_cursor": "after"}}
        with patch.object(self.client, "_make_request", side_effect=[first, second]) as request:
            with patch.object(self.client, "_parse_author", side_effect=lambda row: row):
                result = self.client.fetch_author_batch(batch_size=250)
        self.assertEqual(result["count"], 250)
        self.assertEqual(result["next_cursor"], "after")
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
