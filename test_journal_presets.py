import unittest

from journal_presets import (
    DEFAULT_JOURNAL_PRESET_CONFIG,
    JOURNAL_PRESET_FIELDS,
    normalize_journal_preset_config,
)


class JournalPresetTests(unittest.TestCase):
    def test_normalize_keeps_only_supported_fields(self):
        config = {
            "name": "Example Journal",
            "issn": 1234,
            "scope": None,
            "unexpected": "ignored",
        }

        normalized = normalize_journal_preset_config(config)

        self.assertEqual(set(normalized.keys()), set(JOURNAL_PRESET_FIELDS))
        self.assertEqual(normalized["name"], "Example Journal")
        self.assertEqual(normalized["issn"], "1234")
        self.assertEqual(normalized["scope"], "")
        self.assertNotIn("unexpected", normalized)

    def test_default_invitation_goal_is_restored_when_blank(self):
        normalized = normalize_journal_preset_config({"invitation_goal": ""})

        self.assertEqual(
            normalized["invitation_goal"],
            DEFAULT_JOURNAL_PRESET_CONFIG["invitation_goal"],
        )


if __name__ == "__main__":
    unittest.main()
