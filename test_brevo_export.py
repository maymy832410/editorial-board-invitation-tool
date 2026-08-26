import unittest
from pathlib import Path

from brevo_export import (
    BREVO_CSV_FIELDS,
    build_brevo_export_count_sql,
    build_brevo_export_rows_sql,
    dedupe_brevo_export_rows,
    format_brevo_csv_row,
    normalize_brevo_export_filters,
    row_is_eligible,
    summarize_brevo_export_rows,
    write_brevo_csv,
)


def _row(**overrides):
    row = {
        "email": "ready@example.com",
        "author_name": "Ready Author",
        "orcid_id": "0000-0001",
        "country": "US",
        "discipline": "Medicine",
        "is_suppressed": False,
        "is_retracted": False,
        "is_invited": False,
        "source_priority": 1,
    }
    row.update(overrides)
    return row


class BrevoExportFilterTests(unittest.TestCase):
    def test_default_filters_exclude_suppressed_and_retracted(self):
        filters = normalize_brevo_export_filters()
        rows = [
            _row(),
            _row(email="suppressed@example.com", author_name="Suppressed", is_suppressed=True),
            _row(email="retracted@example.com", author_name="Retracted", is_retracted=True),
        ]

        eligible = [row["email"] for row in rows if row_is_eligible(row, filters)]
        counts = summarize_brevo_export_rows(rows, filters)

        self.assertEqual(eligible, ["ready@example.com"])
        self.assertEqual(counts["eligible"], 1)
        self.assertEqual(counts["excluded_suppressed"], 1)
        self.assertEqual(counts["excluded_retracted"], 1)

    def test_include_suppressed_keeps_those_rows(self):
        filters = normalize_brevo_export_filters(include_suppressed=True)
        suppressed = _row(email="suppressed@example.com", is_suppressed=True)

        self.assertTrue(row_is_eligible(suppressed, filters))
        self.assertEqual(summarize_brevo_export_rows([suppressed], filters)["excluded_suppressed"], 0)

    def test_country_include_and_exclude_both_apply(self):
        include_us = normalize_brevo_export_filters(include_countries=["US"])
        exclude_de = normalize_brevo_export_filters(exclude_countries=["DE"])
        us_row = _row(email="us@example.com", country="US")
        de_row = _row(email="de@example.com", country="DE")
        unknown = _row(email="none@example.com", country="")

        self.assertTrue(row_is_eligible(us_row, include_us))
        self.assertFalse(row_is_eligible(de_row, include_us))
        self.assertFalse(row_is_eligible(unknown, include_us))
        self.assertTrue(row_is_eligible(us_row, exclude_de))
        self.assertFalse(row_is_eligible(de_row, exclude_de))
        self.assertTrue(row_is_eligible(unknown, exclude_de))

        counts = summarize_brevo_export_rows(
            [us_row, de_row, unknown],
            normalize_brevo_export_filters(include_countries=["US"], exclude_countries=["DE"]),
        )
        self.assertEqual(counts["eligible"], 1)
        self.assertEqual(counts["excluded_country"], 2)

    def test_dedupe_prefers_profiles_over_harvested(self):
        rows = [
            _row(
                email="shared@example.com",
                author_name="Harvested Name",
                country="DE",
                discipline="Chemistry",
                source_priority=2,
            ),
            _row(
                email="SHARED@example.com",
                author_name="Profile Name",
                country="US",
                discipline="Medicine",
                source_priority=1,
            ),
        ]

        deduped = dedupe_brevo_export_rows(rows)

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["author_name"], "Profile Name")
        self.assertEqual(deduped[0]["country"], "US")


class BrevoExportCsvTests(unittest.TestCase):
    def test_csv_header_and_full_name_firstname(self):
        csv_text = write_brevo_csv(
            [
                {
                    "email": "ahmed.hassan@university.edu",
                    "author_name": "Ahmed Hassan",
                    "orcid_id": "0000-0002-1234-5678",
                    "country": "IQ",
                    "discipline": "Medicine",
                }
            ]
        )
        lines = [line for line in csv_text.strip().splitlines() if line]

        self.assertEqual(lines[0], ",".join(BREVO_CSV_FIELDS))
        self.assertEqual(
            format_brevo_csv_row(
                {
                    "email": "ahmed.hassan@university.edu",
                    "author_name": "Ahmed Hassan",
                    "orcid_id": "0000-0002-1234-5678",
                    "country": "IQ",
                    "discipline": "Medicine",
                }
            )["FIRSTNAME"],
            "Ahmed Hassan",
        )
        self.assertIn("Ahmed Hassan", lines[1])
        self.assertNotIn("CONTACT ID", csv_text)
        self.assertNotIn("WHATSAPP", csv_text)

    def test_write_brevo_csv_keeps_email_when_rows_already_formatted(self):
        csv_text = write_brevo_csv(
            [
                {
                    "EMAIL": "ahmed.hassan@university.edu",
                    "FIRSTNAME": "Ahmed Hassan",
                    "ORCID": "0000-0002-1234-5678",
                    "COUNTRY": "IQ",
                    "DISCIPLINE": "Medicine",
                }
            ]
        )

        self.assertIn("ahmed.hassan@university.edu", csv_text)
        self.assertIn("Ahmed Hassan", csv_text)


class BrevoExportSqlTests(unittest.TestCase):
    def test_default_sql_excludes_suppressed_and_retracted(self):
        filters = normalize_brevo_export_filters()
        count_sql, count_params = build_brevo_export_count_sql(filters)
        rows_sql, row_params = build_brevo_export_rows_sql(filters)

        self.assertIn("DISTINCT ON (email_lower)", count_sql)
        self.assertIn("DISTINCT ON (email_lower)", rows_sql)
        self.assertIn("NOT is_suppressed", rows_sql)
        self.assertIn("NOT is_retracted", rows_sql)
        self.assertIn("author_profiles", rows_sql)
        self.assertIn("harvested_authors", rows_sql)
        self.assertIn(False, count_params)
        self.assertEqual(row_params[-9:-6], [False, False, False])

    def test_sql_includes_country_filter_params(self):
        filters = normalize_brevo_export_filters(
            include_countries=["US"],
            exclude_countries=["DE"],
        )
        sql, params = build_brevo_export_rows_sql(filters)

        self.assertIn("UPPER(COALESCE(country, '')) = ANY(%s)", sql)
        self.assertIn("UPPER(COALESCE(country, '')) <> ALL(%s)", sql)
        self.assertIn(["US"], params)
        self.assertIn(["DE"], params)


class BrevoExportV1AppTests(unittest.TestCase):
    def test_streamlit_app_exposes_broadcast_workspace(self):
        source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        self.assertIn("def render_brevo_export_panel", source)
        self.assertIn('"Broadcast"', source)
        self.assertIn("render_brevo_export_panel()", source)


if __name__ == "__main__":
    unittest.main()
