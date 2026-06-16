import unittest

from email_sender import EmailSender


class EmailRenderingTests(unittest.TestCase):
    def test_submission_portal_value_stays_on_new_line(self):
        html = EmailSender.__new__(EmailSender)._format_body_html(
            "Scope note: Cardiovascular science.\n\n"
            "Submission portal:\n"
            "Associate Professor Dr. Abdel-Hameed Al-Mistarehi\n\n"
            "Journal website:\n"
            "https://example.com"
        )

        self.assertIn("Submission portal:<br>Associate Professor", html)
        self.assertNotIn("Submission portal: Associate Professor", html)
        self.assertIn("Journal website:<br>", html)


if __name__ == "__main__":
    unittest.main()
