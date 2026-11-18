from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailbackup.events import (
    Occurrence,
    Record,
    parse_jsonl_bytes,
    read_jsonl,
    replay,
    write_jsonl,
)
from tests.support import ROOT  # noqa: F401  (sets sys.path)

C1 = "20261118T100000.000001Z"
C2 = "20261118T100500.000002Z"
C3 = "20261118T101000.000003Z"


def add(cycle, uid, key, flags=(), msgid="", size=1):
    return Record.add(
        cycle,
        "INBOX",
        Occurrence(uid=uid, key=key, flags=flags, message_id=msgid, size=size),
    )


class ReplayTests(unittest.TestCase):
    def test_add_and_remove(self):
        records = [add(C1, 1, "a"), add(C1, 2, "b"), Record.remove(C2, "INBOX", 1, "a")]
        self.assertEqual([occ.uid for occ in replay(records)], [2])

    def test_order_is_arrival_order(self):
        records = [add(C1, 2, "b"), add(C1, 1, "a")]
        self.assertEqual([occ.uid for occ in replay(records)], [2, 1])

    def test_reset_replaces_prior_state(self):
        records = [
            add(C1, 1, "a"),
            Record.reset(C2, "INBOX", 5, [Occurrence(uid=9, key="z")]),
            add(C3, 10, "q"),
        ]
        self.assertEqual([occ.uid for occ in replay(records)], [9, 10])

    def test_reset_is_the_baseline_for_following_ops(self):
        records = [
            Record.reset(C1, "INBOX", 1, [Occurrence(uid=1, key="a")]),
            Record.remove(C2, "INBOX", 1, "a"),
            add(C2, 2, "b"),
        ]
        self.assertEqual([occ.key for occ in replay(records)], ["b"])

    def test_cutoff_is_inclusive(self):
        records = [add(C1, 1, "a"), add(C2, 2, "b")]
        self.assertEqual([occ.uid for occ in replay(records, cutoff=C1)], [1])
        self.assertEqual([occ.uid for occ in replay(records, cutoff=C2)], [1, 2])

    def test_keep_removed_keeps_reset_items_and_adds(self):
        records = [
            Record.reset(C1, "INBOX", 1, [Occurrence(uid=1, key="a")]),
            Record.remove(C2, "INBOX", 1, "a"),
            add(C3, 2, "b"),
        ]
        self.assertEqual([occ.key for occ in replay(records, keep_removed=True)], ["a", "b"])

    def test_keep_removed_dedupes_reset_and_readd(self):
        records = [
            Record.reset(C1, "INBOX", 1, [Occurrence(uid=1, key="a")]),
            Record.reset(C2, "INBOX", 2, [Occurrence(uid=2, key="a")]),
            add(C3, 3, "a"),
        ]
        self.assertEqual(len(replay(records, keep_removed=True)), 1)

    def test_duplicate_content_in_one_folder_is_two_occurrences(self):
        records = [
            Record.reset(C1, "INBOX", 1, []),
            add(C1, 1, "same"),
            add(C1, 2, "same"),
        ]
        self.assertEqual(len(replay(records)), 2)
        self.assertEqual(len(replay(records, keep_removed=True)), 1)


class SerialisationTests(unittest.TestCase):
    def test_roundtrip_all_record_types(self):
        records = [
            Record.reset(
                C1,
                "INBOX",
                7,
                [
                    Occurrence(1, "a" * 64, (r"\Seen",), "<x@y>", 42),
                    Occurrence(2, "b" * 64),
                ],
            ),
            add(C2, 3, "c" * 64, flags=(r"\Flagged",)),
            Record.remove(C2, "INBOX", 1, "a" * 64),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "INBOX-00000000.jsonl"
            write_jsonl(path, records)
            loaded = read_jsonl(path)
        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded[0].type, "reset")
        self.assertEqual(loaded[0].uidvalidity, 7)
        self.assertEqual(loaded[0].items[0].message_id, "<x@y>")
        self.assertEqual(loaded[0].items[0].size, 42)
        self.assertEqual(loaded[1].flags, (r"\Flagged",))
        self.assertEqual(loaded[2].key, "a" * 64)

    def test_unknown_schema_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_jsonl_bytes(b'{"v":99,"t":"add","c":"x","f":"INBOX"}\n')

    def test_malformed_line_reports_origin(self):
        with self.assertRaises(ValueError) as raised:
            parse_jsonl_bytes(b"{not json}\n", origin="log.jsonl")
        self.assertIn("log.jsonl:1", str(raised.exception))

    def test_compact_json(self):
        record = add(C1, 1, "a")
        payload = json.loads(record.to_json())
        self.assertEqual(payload["t"], "add")
        self.assertNotIn(" ", record.to_json())


if __name__ == "__main__":
    unittest.main()
