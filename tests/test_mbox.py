from __future__ import annotations

import mailbox
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mailbackup.events import Occurrence
from mailbackup.mbox import message_to_mbox, render_mbox
from tests.support import ROOT  # noqa: F401


def raw(subject: bytes, body: bytes = b"hello\r\n") -> bytes:
    return (
        b"From: Alice <alice@example.com>\r\n"
        b"Date: Tue, 18 Nov 2026 09:00:00 -0600\r\n"
        b"Subject: " + subject + b"\r\n"
        b"Message-ID: <" + subject + b"@example.com>\r\n"
        b"\r\n" + body
    )


class MboxTests(unittest.TestCase):
    def test_roundtrip_through_python_mailbox(self):
        blobs = {
            "a": raw(b"one", b"hello\r\nFrom the start\r\n>From quoted\r\n"),
            "b": raw(b"two", b"second\r\n"),
        }
        occurrences = [
            Occurrence(1, "a", (r"\Seen", r"\Answered")),
            Occurrence(2, "b"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "INBOX.mbox"
            count, size, digest = render_mbox(
                occurrences,
                blobs.__getitem__,
                path,
                datetime(2026, 11, 18, tzinfo=UTC),
            )
            self.assertEqual(count, 2)
            self.assertGreater(size, 0)
            self.assertEqual(len(digest), 64)
            box = mailbox.mbox(str(path), create=False)
            try:
                self.assertEqual(len(box), 2)
                first = box.get(0)
                self.assertEqual(first.get("Subject"), "one")
                self.assertEqual(first.get("Status"), "R")
                self.assertEqual(first.get("X-Status"), "A")
                self.assertIn(b">From the start", first.get_payload(decode=True))
            finally:
                box.close()

    def test_existing_status_headers_are_replaced(self):
        message = raw(b"x")
        message = message.replace(b"Subject: x", b"Subject: x\r\nStatus: R\r\nX-Status: AF")
        rendered = message_to_mbox(
            message, (r"\Seen",), datetime(2026, 11, 18, tzinfo=UTC)
        )
        self.assertEqual(rendered.count(b"Status: R"), 1)
        self.assertNotIn(b"X-Status: AF", rendered)

    def test_missing_date_falls_back_to_restore_time(self):
        message = b"From: a@example.com\r\nSubject: no date\r\n\r\nbody\r\n"
        rendered = message_to_mbox(
            message, (), datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
        )
        self.assertTrue(rendered.startswith(b"From a@example.com Wed Nov 18 12:00:00 2026\n"))

    def test_empty_render_creates_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.mbox"
            count, size, _ = render_mbox(
                [], lambda key: b"", path, datetime.now(UTC)
            )
            self.assertEqual((count, size), (0, 0))
            self.assertFalse(path.exists())

    def test_crlf_is_normalised(self):
        rendered = message_to_mbox(
            raw(b"crlf", b"a\r\nb\r\n"), (), datetime.now(UTC)
        )
        self.assertNotIn(b"\r\n", rendered)


if __name__ == "__main__":
    unittest.main()
