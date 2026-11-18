from __future__ import annotations

import unittest
from datetime import UTC

from mailbackup.util import (
    folder_dir_name,
    log_file_name,
    parse_log_file_name,
    parse_when,
    safe_stem,
)
from tests.support import ROOT  # noqa: F401


class TimestampTests(unittest.TestCase):
    def test_date_means_end_of_day(self):
        parsed = parse_when("2026-11-18")
        self.assertEqual(parsed.microsecond, 999999)
        self.assertEqual(parsed.hour, 23)
        self.assertEqual(parsed.tzinfo, UTC)

    def test_whole_second_is_inclusive(self):
        parsed = parse_when("2026-11-18T10:00:00Z")
        self.assertEqual(parsed.microsecond, 999999)
        self.assertEqual(parsed.hour, 10)

    def test_fractional_second_is_exact(self):
        parsed = parse_when("2026-11-18T10:00:00.5Z")
        self.assertEqual(parsed.microsecond, 500000)

    def test_internal_cycle_name_round_trips(self):
        parsed = parse_when("20261118T100000.123456Z")
        self.assertEqual(parsed.microsecond, 123456)
        self.assertEqual(parsed.day, 18)

    def test_timezone_offsets_are_normalised(self):
        parsed = parse_when("2026-11-18T10:00:00-06:00")
        self.assertEqual(parsed.hour, 16)


class FolderNameTests(unittest.TestCase):
    def test_nested_folder_is_encoded(self):
        self.assertEqual(folder_dir_name("Read/Reddit"), "Read%2FReddit")

    def test_parent_directory_name_is_neutralised(self):
        self.assertEqual(folder_dir_name(".."), "%2E.")
        self.assertEqual(folder_dir_name("."), "%2E")

    def test_leading_dot_is_encoded(self):
        self.assertTrue(folder_dir_name(".hidden").startswith("%2E"))

    def test_safe_stem_removes_path_separators(self):
        used: set[str] = set()
        self.assertEqual(safe_stem("Read/Reddit", used), "Read_Reddit")
        self.assertEqual(safe_stem("Read/Reddit", used), "Read_Reddit-2")


if __name__ == "__main__":
    unittest.main()


class LogNameTests(unittest.TestCase):
    def test_plain_folder(self):
        self.assertEqual(log_file_name("INBOX", 0), "INBOX-00000000.jsonl")
        self.assertEqual(
            parse_log_file_name("INBOX-00000042.jsonl"), ("INBOX", 42)
        )

    def test_nested_and_spaced_folders(self):
        name = log_file_name("Read/Reddit", 3)
        self.assertEqual(name, "Read%2FReddit-00000003.jsonl")
        self.assertEqual(parse_log_file_name(name), ("Read/Reddit", 3))
        self.assertEqual(
            parse_log_file_name("Sent%20Items-00000000.jsonl"),
            ("Sent Items", 0),
        )

    def test_folder_name_that_looks_like_a_sequence(self):
        name = log_file_name("A-00000000", 1)
        self.assertEqual(name, "A-00000000-00000001.jsonl")
        self.assertEqual(parse_log_file_name(name), ("A-00000000", 1))

    def test_non_log_names_are_ignored(self):
        self.assertIsNone(parse_log_file_name("INBOX.jsonl"))
        self.assertIsNone(parse_log_file_name("INBOX-1.jsonl"))
        self.assertIsNone(parse_log_file_name("state.json"))
