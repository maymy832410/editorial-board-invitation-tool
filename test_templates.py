import unittest

from templates import TEMPLATE_PUBLICATION_RECENT_WORK, format_template


class TemplateFormattingTests(unittest.TestCase):
    def test_publication_details_are_structured_and_not_indexed_is_rewritten(self):
        formatted = format_template(
            template_id=TEMPLATE_PUBLICATION_RECENT_WORK,
            author_name="Example Author",
            journal_name="Babylonian Journal of Internet of Things",
            journal_issn="",
            journal_link="https://example.com",
            editor_in_chief_name="",
            publisher_name="Publisher",
            sender_email="editor@example.com",
            journal_submission_link="Associate Professor Dr. Abdel-Hameed Al-Mistarehi",
            journal_indexing_status="Not indexed",
            author_specialty="IoT",
            journal_scope="IoT Infrastructure and Architectures, IoT Security",
            invitation_goal="Regular submission",
        )

        body = formatted["body"]
        self.assertNotIn("Not indexed", body)
        self.assertIn(
            "Indexed in Google Scholar, Scilit, Dimensions, Semantic Scholar, ISSN, and Crossref.",
            body,
        )
        self.assertIn("Journal details:\n", body)
        self.assertIn("\nCurrent invitation focus:\nRegular submission.", body)
        self.assertIn("\nScope note:\nIoT Infrastructure", body)
        self.assertIn(
            "Submission portal:\nAssociate Professor Dr. Abdel-Hameed Al-Mistarehi",
            body,
        )


if __name__ == "__main__":
    unittest.main()
