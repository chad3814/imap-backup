from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3
from moto import mock_aws

from mailbackup.backend import S3Backend
from mailbackup.backup import BackupEngine
from mailbackup.config import Config
from mailbackup.restore import RestoreEngine
from mailbackup.store import LocalStore
from tests.support import ROOT, FakeImap, make_raw  # noqa: F401


class S3BackendTests(unittest.TestCase):
    def setUp(self):
        self._mock = mock_aws()
        self._mock.start()
        self.raw = boto3.client("s3", region_name="us-east-1")
        self.raw.create_bucket(Bucket="mail-bucket")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.backend = S3Backend(
            "mail-bucket", prefix="archive/mail", region="us-east-1"
        )

    def tearDown(self):
        self._mock.stop()
        self._tmp.cleanup()

    def test_put_get_list_exists_and_prefix(self):
        self.backend.put_bytes("state.json", b'{"last_cycle":""}')
        self.assertTrue(self.backend.exists("state.json"))
        self.assertEqual(self.backend.get_bytes("state.json"), b'{"last_cycle":""}')
        self.assertIn("state.json", self.backend.list_keys())

        self.raw.put_object(Bucket="mail-bucket", Key="elsewhere/x", Body=b"no")
        self.assertNotIn("elsewhere/x", self.backend.list_keys())

        local = self.tmp / "message.eml"
        local.write_bytes(b"hello")
        self.backend.put_file("messages/aa/message.eml", local)
        target = self.tmp / "out.eml"
        self.backend.get_file("messages/aa/message.eml", target)
        self.assertEqual(target.read_bytes(), b"hello")
        head = self.raw.head_object(
            Bucket="mail-bucket", Key="archive/mail/messages/aa/message.eml"
        )
        self.assertEqual(head["ContentType"], "message/rfc822")

    def test_missing_key_is_false(self):
        self.assertFalse(self.backend.exists("nope.json"))

    def test_end_to_end_backup_and_restore_through_s3(self):
        client = FakeImap()
        client.add_folder("INBOX")
        client.add("INBOX", 1, make_raw("one", msgid="one@x"))
        client.add("INBOX", 2, make_raw("two", msgid="two@x"))

        config = Config(
            imap_host="imap.example.com",
            imap_user="u@example.com",
            imap_password="p",
            data_dir=self.tmp / "data",
            backend="s3",
            s3_bucket="mail-bucket",
            s3_prefix="archive/mail",
            aws_region="us-east-1",
            max_ops_per_file=5000,
            snapshot_every_seconds=86400,
        )
        store = LocalStore(config.data_dir)
        backend = S3Backend(
            bucket=config.s3_bucket,
            prefix=config.s3_prefix,
            region=config.aws_region,
        )
        engine = BackupEngine(config, store, client, backend)
        summary = engine.run_cycle()
        self.assertEqual(summary.added, 2)

        keys = backend.list_keys()
        self.assertIn("state.json", keys)
        self.assertEqual(len([k for k in keys if k.startswith("messages/")]), 2)
        state = json.loads(backend.get_bytes("state.json"))
        self.assertEqual(state["last_cycle"], summary.cycle)

        restore = RestoreEngine(backend, self.tmp / "cache", jobs=2)
        manifest = restore.run(self.tmp / "out")
        self.assertEqual(manifest["folders"]["INBOX"]["messages"], 2)
        self.assertEqual(restore.verify()[1], [])


if __name__ == "__main__":
    unittest.main()
