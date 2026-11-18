from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailbackup.restore import RestoreEngine, RestoreError
from tests.support import ROOT, FakeImap, build_engine, make_raw  # noqa: F401

T0 = datetime(2026, 11, 18, 10, 0, 0, 100000, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T1 + timedelta(minutes=5)
T3 = T0 + timedelta(minutes=3)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.client = FakeImap()
        self.client.add_folder("INBOX")
        self.client.add_folder("Read")
        self.client.add("INBOX", 1, make_raw("one", msgid="one@x"))
        self.client.add("INBOX", 2, make_raw("two", msgid="two@x"))
        self.engine, self.store, self.backend, _ = build_engine(self.tmp, self.client)
        self.engine.run_cycle(now=T0)
        self.restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=4)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_restore_uses_latest_cycle(self):
        self.client.add("Read", 50, make_raw("three", msgid="three@x"))
        self.engine.run_cycle(now=T1)
        result = self.restore.collect()
        self.assertEqual(result.cutoff, self.engine.store.load_state()["last_cycle"])
        self.assertEqual(len(result.folders["INBOX"].occurrences), 2)
        self.assertEqual(len(result.folders["Read"].occurrences), 1)

    def test_point_in_time_excludes_later_messages(self):
        self.client.add("INBOX", 3, make_raw("three", msgid="three@x"))
        self.engine.run_cycle(now=T1)
        early = self.restore.run(self.tmp / "early", at=T0.isoformat())
        self.assertEqual(early["folders"]["INBOX"]["messages"], 2)
        late = self.restore.run(self.tmp / "late", at=T1.isoformat())
        self.assertEqual(late["folders"]["INBOX"]["messages"], 3)

    def test_date_only_timestamp_covers_the_whole_day(self):
        self.client.add("INBOX", 3, make_raw("three", msgid="three@x"))
        self.engine.run_cycle(now=T1)
        result = self.restore.collect(at="2026-11-18")
        self.assertEqual(len(result.folders["INBOX"].occurrences), 3)

    def test_restore_before_any_data_is_empty(self):
        manifest = self.restore.run(self.tmp / "old", at="2020-01-01T00:00:00Z")
        self.assertEqual(manifest["folders"], {})

    def test_keep_removed_shows_both_sides_of_a_move(self):
        self.client.move("INBOX", 1, "Read", 100)
        self.engine.run_cycle(now=T1)
        mirror = self.restore.run(self.tmp / "mirror")
        self.assertEqual(mirror["folders"]["INBOX"]["messages"], 1)
        self.assertEqual(mirror["folders"]["Read"]["messages"], 1)
        kept = self.restore.run(self.tmp / "kept", keep_removed=True)
        self.assertEqual(kept["folders"]["INBOX"]["messages"], 2)
        self.assertEqual(kept["folders"]["Read"]["messages"], 1)

    def test_folder_filter(self):
        self.client.add("Read", 50, make_raw("three", msgid="three@x"))
        self.engine.run_cycle(now=T1)
        manifest = self.restore.run(self.tmp / "only", folders={"Read"})
        self.assertEqual(set(manifest["folders"]), {"Read"})
        with self.assertRaises(RestoreError):
            self.restore.collect(folders={"Nope"})

    def test_limit(self):
        manifest = self.restore.run(self.tmp / "limited", limit=1)
        self.assertEqual(manifest["folders"]["INBOX"]["messages"], 1)

    def test_manifest_sha256_matches_the_mbox(self):
        manifest = self.restore.run(self.tmp / "out")
        entry = manifest["folders"]["INBOX"]
        path = self.tmp / "out" / entry["file"]
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"]
        )

    def test_verify_finds_all_blobs(self):
        result, missing = self.restore.verify()
        self.assertEqual(missing, [])
        self.assertEqual(result.messages, 2)

    def test_missing_blob_fails_restore_unless_allowed(self):
        remote = [k for k in self.backend.list_keys("") if k.startswith("messages/")]
        (self.backend.root / remote[0]).unlink()
        _, missing = self.restore.verify()
        self.assertEqual(len(missing), 1)
        with self.assertRaises(RestoreError):
            self.restore.run(self.tmp / "broken")
        manifest = self.restore.run(self.tmp / "tolerant", allow_missing=True)
        self.assertEqual(len(manifest["missing_blobs"]), 1)
        total = sum(entry["messages"] for entry in manifest["folders"].values())
        self.assertEqual(total, 1)

    def test_corrupt_blob_is_rejected(self):
        remote = [k for k in self.backend.list_keys("") if k.startswith("messages/")]
        (self.backend.root / remote[0]).write_bytes(b"not the message")
        with self.assertRaises(RestoreError):
            self.restore.run(self.tmp / "corrupt")

    def test_manifest_is_written(self):
        self.restore.run(self.tmp / "out")
        manifest = json.loads((self.tmp / "out" / "manifest.json").read_text())
        self.assertEqual(
            manifest["cutoff"], self.engine.store.load_state()["last_cycle"]
        )
        self.assertIn("INBOX", manifest["folders"])
        self.assertEqual(manifest["folders"]["INBOX"]["baseline"], T0.strftime("%Y%m%dT%H%M%S.%fZ"))


class FileSelectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.client = FakeImap()
        self.client.add_folder("INBOX")
        self.client.add("INBOX", 1, make_raw("one", msgid="one@x"))
        self.client.add("INBOX", 2, make_raw("two", msgid="two@x"))
        self.engine, _, self.backend, _ = build_engine(
            self.tmp, self.client, max_ops_per_file=2
        )
        self.engine.run_cycle(now=T0)
        self.client.add("INBOX", 3, make_raw("three", msgid="three@x"))
        self.client.add("INBOX", 4, make_raw("four", msgid="four@x"))
        self.engine.run_cycle(now=T1)
        self.restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=2)

    def tearDown(self):
        self._tmp.cleanup()

    def test_mirror_uses_exactly_the_latest_file_at_or_before_cutoff(self):
        # max_ops_per_file=1: file 0 = reset[1,2] + add 3, file 1 = reset[1,2,3] + add 4
        result = self.restore.collect()
        selection = result.folders["INBOX"]
        self.assertEqual(selection.files_read, 1)
        self.assertEqual(len(selection.occurrences), 4)
        self.assertEqual(
            selection.baseline,
            self.engine.store.load_state()["folders"]["INBOX"]["file_start"],
        )

    def test_point_in_time_before_the_latest_baseline_uses_the_older_file(self):
        result = self.restore.collect(at=(T0 + timedelta(minutes=1)).isoformat())
        selection = result.folders["INBOX"]
        self.assertEqual(len(selection.occurrences), 2)
        self.assertEqual(selection.files_read, 1)

    def test_keep_removed_dedupes_repeated_baselines(self):
        kept = self.restore.run(self.tmp / "kept", keep_removed=True)
        self.assertEqual(kept["folders"]["INBOX"]["messages"], 4)
        self.assertEqual(kept["folders"]["INBOX"]["files_read"], 2)


class RepeatRestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.client = FakeImap()
        self.client.add_folder("INBOX")
        self.client.add("INBOX", 1, make_raw("one", msgid="one@x"))
        self.engine, _, self.backend, _ = build_engine(self.tmp, self.client)
        self.engine.run_cycle(now=T0)
        self.restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=2)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_mbox_from_a_previous_restore_is_removed(self):
        out = self.tmp / "out"
        first = self.restore.run(out)
        self.assertEqual(first["folders"]["INBOX"]["messages"], 1)
        self.assertTrue((out / "INBOX.mbox").exists())

        self.client.delete("INBOX", 1)
        self.engine.run_cycle(now=T1)
        second = self.restore.run(out)
        self.assertEqual(second["folders"]["INBOX"]["messages"], 0)
        self.assertFalse((out / "INBOX.mbox").exists())

    def test_unrelated_files_are_not_removed(self):
        out = self.tmp / "out"
        out.mkdir()
        keep = out / "notes.txt"
        keep.write_text("do not delete")
        self.restore.run(out)
        self.assertTrue(keep.exists())


if __name__ == "__main__":
    unittest.main()
