from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from mailbackup.config import Config
from mailbackup.util import ConfigError
from tests.support import ROOT  # noqa: F401


class PasswordFileTests(unittest.TestCase):
    def test_password_is_read_from_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "imap_password"
            path.write_text("s3cret\n", encoding="utf-8")
            env = {
                "IMAP_HOST": "imap.example.com",
                "IMAP_USER": "u@example.com",
                "IMAP_PASSWORD_FILE": str(path),
                "S3_BUCKET": "bucket",
            }
            with patch.dict(os.environ, env, clear=True):
                config = Config.from_env()
            self.assertEqual(config.imap_password, "s3cret")

    def test_missing_password_file_is_a_config_error(self):
        env = {
            "IMAP_HOST": "imap.example.com",
            "IMAP_USER": "u@example.com",
            "IMAP_PASSWORD_FILE": "/does/not/exist",
            "S3_BUCKET": "bucket",
        }
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigError):
            Config.from_env()

    def test_password_does_not_appear_in_repr(self):
        config = Config(imap_password="hunter2", s3_bucket="bucket")
        self.assertNotIn("hunter2", repr(config))

    def test_environment_password_is_the_fallback(self):
        env = {
            "IMAP_HOST": "imap.example.com",
            "IMAP_USER": "u@example.com",
            "IMAP_PASSWORD": "from-env",
            "S3_BUCKET": "bucket",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config.from_env().imap_password, "from-env")


class ConfigParsingTests(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {
        "IMAP_HOST": "imap.example.com",
        "IMAP_USER": "u@example.com",
        "IMAP_PASSWORD": "p",
        "S3_BUCKET": "bucket",
    }

    def test_folders_are_split_and_trimmed(self):
        env = dict(self.BASE, IMAP_FOLDERS="INBOX, Sent Items ,")
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.imap_folders, ("INBOX", "Sent Items"))

    def test_prefix_is_stripped(self):
        env = dict(self.BASE, S3_PREFIX="/mail/")
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config.from_env().s3_prefix, "mail")

    def test_missing_bucket_is_rejected_for_s3(self):
        env = dict(self.BASE)
        env.pop("S3_BUCKET")
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigError):
            Config.from_env()

    def test_dry_run_allows_a_missing_bucket(self):
        env = dict(self.BASE, DRY_RUN="true")
        env.pop("S3_BUCKET")
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(Config.from_env().dry_run)

    def test_restore_config_does_not_require_imap(self):
        env = {"BACKEND": "dir", "BACKUP_DIR": "/tmp/mirror"}
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env(require_imap=False)
        self.assertEqual(config.backend, "dir")
        self.assertEqual(config.imap_host, "")

    def test_log_caps_are_parsed(self):
        env = dict(
            self.BASE, MAX_OPS_PER_FILE="10", SNAPSHOT_EVERY_SECONDS="60"
        )
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.max_ops_per_file, 10)
        self.assertEqual(config.snapshot_every_seconds, 60)

    def test_small_operation_caps_are_rejected(self):
        for value in ("0", "1"):
            env = dict(self.BASE, MAX_OPS_PER_FILE=value)
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigError):
                Config.from_env()

    def test_invalid_port_is_rejected(self):
        env = dict(self.BASE, IMAP_PORT="not-a-number")
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ConfigError):
            Config.from_env()


if __name__ == "__main__":
    unittest.main()
