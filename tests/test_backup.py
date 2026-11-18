from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailbackup.backup import BackupError
from mailbackup.events import parse_jsonl_bytes
from mailbackup.restore import RestoreEngine
from mailbackup.util import log_file_name, parse_log_file_name
from tests.support import ROOT, FakeImap, build_engine, make_raw  # noqa: F401

T0 = datetime(2026, 11, 18, 10, 0, 0, 100000, tzinfo=UTC)
T1 = T0 + timedelta(minutes=5)
T2 = T0 + timedelta(minutes=10)
T3 = T0 + timedelta(minutes=3)


def by_name(summary):
    return {folder.name: folder for folder in summary.folders}


def log_keys(backend, folder):
    found = []
    for key in backend.list_keys("mboxlogs/"):
        parsed = parse_log_file_name(key.split("/", 1)[1])
        if parsed is not None and parsed[0] == folder:
            found.append((parsed[1], key))
    return sorted(found)


def read_file(backend, folder, seq):
    key = f"mboxlogs/{log_file_name(folder, seq)}"
    return parse_jsonl_bytes(backend.get_bytes(key), origin=key)


def latest_file(backend, folder):
    entries = log_keys(backend, folder)
    if not entries:
        return None
    return read_file(backend, folder, entries[-1][0])


def all_files(backend, folder):
    return [read_file(backend, folder, seq) for seq, _ in log_keys(backend, folder)]


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.client = FakeImap()
        self.client.add_folder("INBOX")
        self.client.add_folder("Read")
        self.client.add_folder("Trash")
        self.client.add("INBOX", 1, make_raw("one", msgid="one@x"))
        self.client.add(
            "INBOX", 2, make_raw("two", msgid="two@x"), frozenset({r"\Seen"})
        )
        self.client.add("Trash", 1, make_raw("junk", msgid="junk@x"))
        self.engine, self.store, self.backend, self.config = build_engine(
            self.tmp, self.client
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_cycle_is_one_reset_baseline(self):
        summary = self.engine.run_cycle(now=T0)
        self.assertEqual({f.name for f in summary.folders}, {"INBOX", "Read"})
        inbox = by_name(summary)["INBOX"]
        self.assertEqual((inbox.added, inbox.removed), (2, 0))
        self.assertTrue(inbox.baseline)

        entries = log_keys(self.backend, "INBOX")
        self.assertEqual([seq for seq, _ in entries], [0])
        self.assertEqual(entries[0][1], "mboxlogs/INBOX-00000000.jsonl")
        records = read_file(self.backend, "INBOX", 0)
        self.assertEqual(records[0].type, "reset")
        self.assertEqual(records[0].uidvalidity, 1)
        self.assertEqual([item.uid for item in records[0].items], [1, 2])
        self.assertEqual(records[0].items[1].flags, (r"\Seen",))
        self.assertEqual(len(records), 1)  # baseline only, no add records
        self.assertEqual(log_keys(self.backend, "Read"), [])  # empty folder

        self.assertEqual(len(list(self.store.iter_blob_keys())), 2)
        state = json.loads(self.backend.get_bytes("state.json"))
        self.assertEqual(state["folders"]["INBOX"]["seq"], 0)
        self.assertEqual(state["folders"]["INBOX"]["ops"], 0)

    def test_second_cycle_with_no_changes_is_a_no_op(self):
        self.engine.run_cycle(now=T0)
        before = self.backend.get_bytes("mboxlogs/INBOX-00000000.jsonl")
        fetched_before = list(self.client.fetched)
        summary = self.engine.run_cycle(now=T1)
        self.assertEqual((summary.added, summary.removed), (0, 0))
        self.assertEqual(
            self.backend.get_bytes("mboxlogs/INBOX-00000000.jsonl"), before
        )
        self.assertEqual(self.client.fetched, fetched_before)

    def test_move_and_delete_append_ops_and_moves_cost_no_blob(self):
        self.engine.run_cycle(now=T0)
        moved_key = hashlib.sha256(make_raw("one", msgid="one@x")).hexdigest()

        self.client.move("INBOX", 1, "Read", 100)
        self.client.delete("INBOX", 2)
        self.client.add("Read", 101, make_raw("three", msgid="three@x"))
        summary = self.engine.run_cycle(now=T1)

        folders = by_name(summary)
        self.assertEqual((folders["INBOX"].added, folders["INBOX"].removed), (0, 2))
        self.assertEqual((folders["Read"].added, folders["Read"].removed), (2, 0))
        self.assertTrue(folders["Read"].baseline)
        self.assertEqual(len(list(self.store.iter_blob_keys())), 3)
        self.assertIn(moved_key, list(self.store.iter_blob_keys()))

        inbox = read_file(self.backend, "INBOX", 0)
        self.assertEqual(inbox[0].type, "reset")
        self.assertEqual([record.type for record in inbox[1:]], ["rm", "rm"])
        self.assertEqual([record.uid for record in inbox[1:]], [1, 2])

        read = read_file(self.backend, "Read", 0)
        self.assertEqual(read[0].type, "reset")
        self.assertEqual([item.uid for item in read[0].items], [100, 101])
        self.assertEqual(read[0].items[1].message_id, "<three@x>")

        restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=4)
        latest = restore.run(self.tmp / "out-latest")
        self.assertEqual(latest["folders"]["INBOX"]["messages"], 0)
        self.assertEqual(latest["folders"]["Read"]["messages"], 2)
        self.assertFalse((self.tmp / "out-latest" / "INBOX.mbox").exists())

        before = restore.run(
            self.tmp / "out-before", at=(T0 + timedelta(minutes=1)).isoformat()
        )
        self.assertEqual(before["folders"]["INBOX"]["messages"], 2)
        self.assertNotIn("Read", before["folders"])

    def test_uidvalidity_change_starts_a_new_baseline_file(self):
        self.engine.run_cycle(now=T0)
        self.client.set_uidvalidity("INBOX", 2)
        self.client.boxes["INBOX"].messages.clear()
        self.client.add("INBOX", 10, make_raw("one", msgid="one@x"))
        self.client.add("INBOX", 11, make_raw("new", msgid="new@x"))
        summary = self.engine.run_cycle(now=T1)

        inbox = by_name(summary)["INBOX"]
        self.assertTrue(inbox.reset)
        self.assertEqual((inbox.added, inbox.removed), (2, 0))
        self.assertEqual([seq for seq, _ in log_keys(self.backend, "INBOX")], [0, 1])
        new_file = read_file(self.backend, "INBOX", 1)
        self.assertEqual(new_file[0].type, "reset")
        self.assertEqual(new_file[0].uidvalidity, 2)
        self.assertEqual([item.uid for item in new_file[0].items], [10, 11])
        self.assertEqual(len(new_file), 1)

        restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=4)
        latest = restore.run(self.tmp / "out")
        self.assertEqual(latest["folders"]["INBOX"]["messages"], 2)
        self.assertEqual(latest["folders"]["INBOX"]["baseline"], new_file[0].cycle)
        kept = restore.run(self.tmp / "out-kept", keep_removed=True)
        self.assertEqual(kept["folders"]["INBOX"]["messages"], 3)

    def test_duplicate_message_ids_with_different_bytes_are_kept(self):
        self.client.add(
            "INBOX",
            3,
            make_raw(
                "two",
                msgid="two@x",
                extra_headers="Received: by mx2; Tue, 18 Nov 2026 10:01:00 -0600\r\n",
            ),
        )
        summary = self.engine.run_cycle(now=T0)
        self.assertEqual(by_name(summary)["INBOX"].added, 3)
        self.assertEqual(len(list(self.store.iter_blob_keys())), 3)
        baseline = read_file(self.backend, "INBOX", 0)
        self.assertEqual(len(baseline[0].items), 3)

        restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=4)
        result = restore.run(self.tmp / "out")
        self.assertEqual(result["folders"]["INBOX"]["messages"], 3)

    def test_identical_bytes_in_two_folders_share_one_blob(self):
        raw = make_raw("shared", msgid="shared@x")
        self.client.add("INBOX", 5, raw)
        self.client.add("Read", 50, raw)
        summary = self.engine.run_cycle(now=T0)
        self.assertEqual(by_name(summary)["INBOX"].added, 3)
        self.assertEqual(by_name(summary)["Read"].added, 1)
        self.assertEqual(len(list(self.store.iter_blob_keys())), 3)
        self.assertEqual(
            read_file(self.backend, "Read", 0)[0].items[0].key,
            read_file(self.backend, "INBOX", 0)[0].items[2].key,
        )

        restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=4)
        result = restore.run(self.tmp / "out")
        self.assertEqual(result["folders"]["INBOX"]["messages"], 3)
        self.assertEqual(result["folders"]["Read"]["messages"], 1)

    def test_nested_folder_name_is_encoded_in_the_filename(self):
        self.client.add_folder("Read/Reddit")
        self.client.add("Read/Reddit", 7, make_raw("nested", msgid="nested@x"))
        self.engine.run_cycle(now=T0)
        keys = self.backend.list_keys("mboxlogs/")
        self.assertIn("mboxlogs/Read%2FReddit-00000000.jsonl", keys)
        restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=4)
        manifest = restore.run(self.tmp / "out")
        self.assertEqual(manifest["folders"]["Read/Reddit"]["messages"], 1)
        self.assertTrue((self.tmp / "out" / "Read_Reddit.mbox").exists())

    def test_nested_folder_with_noselect_parent(self):
        # A hierarchical parent is commonly \Noselect; only the selectable
        # child should be backed up, and its slash must be encoded in the log
        # filename rather than creating a directory.
        self.client.add_folder(
            "Read", flags=frozenset({"Noselect", "HasChildren"})
        )
        self.client.add_folder("Read/Root")
        self.client.add("Read/Root", 7, make_raw("nested", msgid="nested@x"))

        summary = self.engine.run_cycle(now=T0)
        names = {folder.name for folder in summary.folders}
        self.assertIn("Read/Root", names)
        self.assertNotIn("Read", names)

        keys = self.backend.list_keys("mboxlogs/")
        self.assertIn("mboxlogs/Read%2FRoot-00000000.jsonl", keys)
        self.assertFalse(any("/Read/" in key for key in keys))

        restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=4)
        manifest = restore.run(self.tmp / "out")
        self.assertEqual(manifest["folders"]["Read/Root"]["messages"], 1)
        self.assertTrue((self.tmp / "out" / "Read_Root.mbox").exists())
        only = restore.collect(folders={"Read/Root"})
        self.assertEqual(len(only.folders["Read/Root"].occurrences), 1)

    def test_explicit_nested_folder_selection(self):
        self.client.add_folder("Read/Root")
        self.client.add("Read/Root", 7, make_raw("nested", msgid="nested@x"))
        engine, _, backend, _ = build_engine(
            self.tmp / "nested", self.client, imap_folders=("Read/Root",)
        )
        summary = engine.run_cycle(now=T0)
        self.assertEqual([f.name for f in summary.folders], ["Read/Root"])
        self.assertIn(
            "mboxlogs/Read%2FRoot-00000000.jsonl", backend.list_keys("mboxlogs/")
        )

    def test_all_folders_includes_junk_and_trash(self):
        self.client.add_folder("Junk", flags=frozenset({"Junk"}))
        self.client.add("Junk", 1, make_raw("spam", msgid="spam@x"))
        engine, _, _, _ = build_engine(
            self.tmp / "all", self.client, imap_all_folders=True
        )
        summary = engine.run_cycle(now=T0)
        self.assertEqual(
            {f.name for f in summary.folders}, {"INBOX", "Read", "Trash", "Junk"}
        )
        self.assertTrue(log_keys(engine.backend, "Trash"))

    def test_dry_run_writes_nothing(self):
        summary = self.engine.run_cycle(now=T0, dry_run=True)
        self.assertEqual(summary.added, 2)
        self.assertTrue(summary.dry_run)
        self.assertEqual(self.backend.list_keys(""), set())
        self.assertFalse(self.store.state_path.exists())

    def test_folder_selection_error(self):
        engine, _, _, _ = build_engine(
            self.tmp, self.client, imap_folders=("Nope",)
        )
        with self.assertRaises(BackupError):
            engine.run_cycle(now=T0)


class RollingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def client(self, count=1):
        client = FakeImap()
        client.add_folder("INBOX")
        for uid in range(1, count + 1):
            client.add("INBOX", uid, make_raw(f"m{uid}", msgid=f"m{uid}@x"))
        return client

    def test_operation_cap_rolls_mid_cycle(self):
        client = self.client(1)
        engine, _, backend, _ = build_engine(
            self.tmp, client, max_ops_per_file=3
        )
        engine.run_cycle(now=T0)
        for uid in range(2, 7):
            client.add("INBOX", uid, make_raw(f"m{uid}", msgid=f"m{uid}@x"))
        summary = engine.run_cycle(now=T1)
        self.assertEqual(summary.added, 5)

        files = all_files(backend, "INBOX")
        self.assertEqual(len(files), 3)
        for records in files:
            self.assertEqual(records[0].type, "reset")
            self.assertLessEqual(len(records) - 1, 2)
        self.assertEqual([len(f) - 1 for f in files], [2, 2, 1])
        self.assertEqual([item.uid for item in files[1][0].items], [1, 2, 3])
        self.assertEqual([item.uid for item in files[2][0].items], [1, 2, 3, 4, 5])

        restore = RestoreEngine(backend, self.tmp / "cache", jobs=2)
        manifest = restore.run(self.tmp / "out")
        self.assertEqual(manifest["folders"]["INBOX"]["messages"], 6)

    def test_snapshot_interval_ends_a_file_early(self):
        client = self.client(1)
        engine, _, backend, _ = build_engine(
            self.tmp, client, snapshot_every_seconds=60
        )
        engine.run_cycle(now=T0)
        client.add("INBOX", 2, make_raw("m2", msgid="m2@x"))
        summary = engine.run_cycle(now=T3)  # +3 minutes
        self.assertEqual(summary.added, 1)
        self.assertTrue(by_name(summary)["INBOX"].snapshot)

        files = all_files(backend, "INBOX")
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0][0].type, "reset")
        self.assertEqual([record.type for record in files[0][1:]], ["add"])
        self.assertEqual(files[1][0].type, "reset")
        self.assertEqual([item.uid for item in files[1][0].items], [1, 2])
        self.assertEqual(len(files[1]), 1)
        restore = RestoreEngine(backend, self.tmp / "cache", jobs=2)
        manifest = restore.run(self.tmp / "out")
        self.assertEqual(manifest["folders"]["INBOX"]["messages"], 2)

    def test_snapshot_interval_rolls_even_without_new_mail(self):
        client = self.client(1)
        engine, _, backend, _ = build_engine(
            self.tmp, client, snapshot_every_seconds=60
        )
        engine.run_cycle(now=T0)
        summary = engine.run_cycle(now=T3)
        self.assertEqual((summary.added, summary.removed), (0, 0))
        self.assertTrue(by_name(summary)["INBOX"].snapshot)
        files = all_files(backend, "INBOX")
        self.assertEqual(len(files), 2)
        self.assertEqual([item.uid for item in files[1][0].items], [1])


class RestartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_client(self):
        client = FakeImap()
        client.add_folder("INBOX")
        client.add("INBOX", 1, make_raw("one", msgid="one@x"))
        return client

    def test_fresh_volume_bootstraps_from_the_numbered_log(self):
        client = self.make_client()
        engine, _, _backend, _ = build_engine(self.tmp, client)
        engine.run_cycle(now=T0)

        import shutil

        shutil.rmtree(self.tmp / "data")

        client.add("INBOX", 2, make_raw("two", msgid="two@x"))
        engine2, store2, backend2, _ = build_engine(self.tmp, client)
        summary = engine2.run_cycle(now=T1)
        self.assertEqual(summary.added, 1)
        self.assertEqual(client.fetched, [1, 2])
        self.assertEqual(len(list(store2.iter_blob_keys())), 1)
        self.assertEqual(
            len([k for k in backend2.list_keys("") if k.startswith("messages/")]),
            2,
        )
        latest = latest_file(backend2, "INBOX")
        self.assertEqual(latest[0].type, "reset")
        self.assertEqual([item.uid for item in latest[0].items], [1])
        self.assertEqual(latest[-1].type, "add")
        self.assertEqual(latest[-1].uid, 2)

        restore = RestoreEngine(backend2, self.tmp / "cache", jobs=4)
        result = restore.run(self.tmp / "out")
        self.assertEqual(result["folders"]["INBOX"]["messages"], 2)

    def test_missing_state_resumes_from_the_log(self):
        client = self.make_client()
        engine, _, backend, _ = build_engine(self.tmp, client)
        engine.run_cycle(now=T0)
        (self.tmp / "data" / "state.json").unlink()
        (backend.root / "state.json").unlink()

        client.add("INBOX", 2, make_raw("two", msgid="two@x"))
        engine2, _, backend2, _ = build_engine(self.tmp, client)
        summary = engine2.run_cycle(now=T1)
        self.assertEqual(summary.added, 1)
        latest = latest_file(backend2, "INBOX")
        self.assertEqual([record.type for record in latest[1:]], ["add"])
        self.assertEqual(latest[-1].uid, 2)

    def test_interrupted_upload_is_retried_on_next_start(self):
        client = self.make_client()
        engine, _, backend, _ = build_engine(self.tmp, client)
        engine.run_cycle(now=T0)
        for key in list(backend.list_keys("")):
            if key != "state.json":
                (backend.root / key).unlink()
        engine2, _, backend2, _ = build_engine(self.tmp, client)
        summary = engine2.run_cycle(now=T1)
        self.assertEqual(summary.added, 0)
        keys = backend2.list_keys("")
        self.assertTrue(any(k.startswith("messages/") for k in keys))
        self.assertIn("mboxlogs/INBOX-00000000.jsonl", keys)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.client = FakeImap()
        self.client.add_folder("INBOX")
        self.client.add("INBOX", 1, make_raw("one", msgid="one@x"))
        self.client.add("INBOX", 2, make_raw("two", msgid="two@x"))
        self.engine, _, self.backend, _ = build_engine(self.tmp, self.client)
        self.engine.run_cycle(now=T0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_search_for_nonempty_folder_is_not_a_mass_delete(self):
        before = self.backend.get_bytes("mboxlogs/INBOX-00000000.jsonl")

        def broken_search():
            return []

        self.client.search_all_uids = broken_search
        summary = self.engine.run_cycle(now=T1)
        self.assertIsNotNone(by_name(summary)["INBOX"].error)
        self.assertEqual(
            self.backend.get_bytes("mboxlogs/INBOX-00000000.jsonl"), before
        )
        restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=2)
        result = restore.run(self.tmp / "out")
        self.assertEqual(result["folders"]["INBOX"]["messages"], 2)

    def test_partial_search_logs_a_warning_but_keeps_going(self):
        self.client.search_all_uids = lambda: [1]
        summary = self.engine.run_cycle(now=T1)
        inbox = by_name(summary)["INBOX"]
        self.assertIsNone(inbox.error)
        self.assertEqual(inbox.removed, 1)


if __name__ == "__main__":
    unittest.main()


class CrashDurabilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.client = FakeImap()
        self.client.add_folder("INBOX")
        self.client.add("INBOX", 1, make_raw("one", msgid="one@x"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_remote_older_active_file_is_reuploaded_after_restart(self):
        engine, _, backend, _ = build_engine(self.tmp, self.client)
        engine.run_cycle(now=T0)
        old_remote = backend.get_bytes("mboxlogs/INBOX-00000000.jsonl")

        self.client.add("INBOX", 2, make_raw("two", msgid="two@x"))
        engine.run_cycle(now=T1)

        # Simulate the cycle-2 log upload having been lost: the key still
        # exists, but holds the shorter cycle-1 version.
        (backend.root / "mboxlogs/INBOX-00000000.jsonl").write_bytes(old_remote)

        engine2, _, backend2, _ = build_engine(self.tmp, self.client)
        summary = engine2.run_cycle(now=T2)
        self.assertEqual(summary.added, 0)
        latest = latest_file(backend2, "INBOX")
        self.assertEqual([record.type for record in latest[1:]], ["add"])
        self.assertEqual(latest[-1].uid, 2)
        restore = RestoreEngine(backend2, self.tmp / "cache", jobs=2)
        result = restore.run(self.tmp / "out")
        self.assertEqual(result["folders"]["INBOX"]["messages"], 2)
