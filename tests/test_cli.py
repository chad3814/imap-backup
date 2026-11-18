from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailbackup.cli import main
from tests.support import ROOT  # noqa: F401


def run_cli(args: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(args)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_version(self):
        code, out, _ = run_cli(["version"])
        self.assertEqual(code, 0)
        self.assertIn("mailbackup", out)

    def test_health_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            from mailbackup.util import cycle_name, utcnow

            Path(tmp, "state.json").write_text(
                json.dumps({"last_cycle": cycle_name(utcnow())}),
                encoding="utf-8",
            )
            code, out, _ = run_cli(["health", "--data-dir", tmp])
        self.assertEqual(code, 0)
        self.assertIn("ok:", out)

    def test_health_fails_without_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_cli(["health", "--data-dir", tmp])
        self.assertEqual(code, 1)
        self.assertIn("unhealthy", err)

    def test_health_fails_when_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "state.json").write_text(
                json.dumps({"last_cycle": "20200101T000000.000000Z"}),
                encoding="utf-8",
            )
            code, _, err = run_cli(
                ["health", "--data-dir", tmp, "--max-age", "60"]
            )
        self.assertEqual(code, 1)
        self.assertIn("unhealthy", err)

    def test_missing_config_is_exit_2(self):
        with patch.dict(os.environ, {}, clear=True):
            code, _, err = run_cli(["backup", "--backend", "null"])
        self.assertEqual(code, 2)
        self.assertIn("configuration error", err)

    def test_bad_timestamp_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"BACKEND": "dir", "BACKUP_DIR": tmp}
            with patch.dict(os.environ, env, clear=True):
                code, _, err = run_cli(
                    ["restore", "--out", str(Path(tmp, "out")), "--at", "not-a-date"]
                )
        self.assertEqual(code, 2)
        self.assertIn("error:", err)


if __name__ == "__main__":
    unittest.main()
