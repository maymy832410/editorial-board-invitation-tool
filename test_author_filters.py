import unittest

from author_filters import author_matches_any_specialty, dedupe_authors


class AuthorFilterTests(unittest.TestCase):
    def test_no_selected_specialties_matches_all_authors(self):
        self.assertTrue(author_matches_any_specialty({"specialty": "IoT"}, []))

    def test_selected_specialty_matches_author_specialty(self):
        author = {"specialty": "Internet of Things", "all_topics": []}

        self.assertTrue(author_matches_any_specialty(author, ["Internet of Things"]))

    def test_selected_specialty_matches_any_author_topic(self):
        author = {"specialty": "Computer Science", "all_topics": ["IoT", "Sensors"]}

        self.assertTrue(author_matches_any_specialty(author, ["IoT"]))

    def test_multiple_selected_specialties_use_or_matching(self):
        author = {"specialty": "Cybersecurity", "all_topics": ["Privacy"]}

        self.assertTrue(author_matches_any_specialty(author, ["IoT", "Privacy"]))

    def test_non_selected_specialty_is_excluded(self):
        author = {"specialty": "Robotics", "all_topics": ["Automation"]}

        self.assertFalse(author_matches_any_specialty(author, ["IoT", "Privacy"]))

    def test_specialty_matching_is_case_insensitive_and_partial(self):
        author = {"specialty": "Clinical Oncology", "all_topics": []}
        self.assertTrue(author_matches_any_specialty(author, ["oncology"]))

    def test_specialty_matching_uses_subfield_and_research_areas(self):
        author = {"subfield": "Machine Learning", "research_areas": "Neural Networks"}
        self.assertTrue(author_matches_any_specialty(author, ["neural"]))

    def test_dedupe_prefers_orcid(self):
        authors = [
            {"name": "First", "orcid_id": "0000-0001", "email": "one@example.com"},
            {"name": "Duplicate", "orcid_id": "0000-0001", "email": "other@example.com"},
        ]

        self.assertEqual([a["name"] for a in dedupe_authors(authors)], ["First"])

    def test_dedupe_falls_back_to_email(self):
        authors = [
            {"name": "First", "orcid_id": "", "email": "ONE@example.com"},
            {"name": "Duplicate", "orcid_id": "", "email": "one@example.com"},
        ]

        self.assertEqual([a["name"] for a in dedupe_authors(authors)], ["First"])

    def test_dedupe_falls_back_to_openalex_or_profile_key(self):
        authors = [
            {"name": "OpenAlex First", "author_id": "https://openalex.org/A1"},
            {"name": "OpenAlex Duplicate", "author_id": "https://openalex.org/A1/"},
            {"name": "Profile First", "profile_key": "profile:abc"},
            {"name": "Profile Duplicate", "profile_key": "profile:abc"},
        ]

        self.assertEqual(
            [a["name"] for a in dedupe_authors(authors)],
            ["OpenAlex First", "Profile First"],
        )


if __name__ == "__main__":
    unittest.main()
