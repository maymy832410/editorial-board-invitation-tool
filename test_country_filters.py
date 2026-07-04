import unittest

from openalex_client import OpenAlexClient


class CountryFilterTests(unittest.TestCase):
    def setUp(self):
        self.client = OpenAlexClient()

    def test_one_included_country(self):
        result = self.client.build_filter(include_country_codes=["US"])
        self.assertIn("last_known_institutions.country_code:US", result)

    def test_multiple_included_countries_use_or(self):
        result = self.client.build_filter(include_country_codes=["US", "GB"])
        self.assertIn("last_known_institutions.country_code:GB|US", result)

    def test_inclusion_wins_over_exclusion(self):
        result = self.client.build_filter(
            include_country_codes=["US"], exclude_country_codes=["US", "DE"]
        )
        self.assertIn("country_code:US", result)
        self.assertIn("country_code:!DE", result)
        self.assertNotIn("!US", result)

    def test_no_inclusion_preserves_existing_filter(self):
        without_new_argument = self.client.build_filter(exclude_country_codes=["DE"])
        with_empty_inclusion = self.client.build_filter(
            include_country_codes=[], exclude_country_codes=["DE"]
        )
        self.assertEqual(without_new_argument, with_empty_inclusion)


if __name__ == "__main__":
    unittest.main()
