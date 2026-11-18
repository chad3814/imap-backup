from __future__ import annotations

import mailbox
import socketserver
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mailbackup.imap import ImapClient
from mailbackup.restore import RestoreEngine
from tests.support import ROOT, build_engine  # noqa: F401

RAW_ONE = (
    b"From: Alice <alice@example.com>\r\n"
    b"Date: Tue, 18 Nov 2026 09:00:00 -0600\r\n"
    b"Subject: first\r\n"
    b"Message-ID: <first@example.com>\r\n"
    b"\r\n"
    b"hello\r\nFrom the start\r\n"
)
RAW_TWO = (
    b"From: Bob <bob@example.com>\r\n"
    b"Date: Tue, 18 Nov 2026 10:00:00 -0600\r\n"
    b"Subject: second\r\n"
    b"Message-ID: <second@example.com>\r\n"
    b"\r\n"
    b"second\r\n"
)


class MiniImapHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"* OK mini imap ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            self.server.commands.append(text)
            tag, _, rest = text.partition(" ")
            command, _, args = rest.partition(" ")
            command = command.upper()
            tag_bytes = tag.encode()

            if command == "CAPABILITY":
                self.wfile.write(
                    b"* CAPABILITY IMAP4rev1\r\n" + tag_bytes + b" OK done\r\n"
                )
            elif command == "LOGIN":
                self.wfile.write(tag_bytes + b" OK logged in\r\n")
            elif command == "NOOP":
                self.wfile.write(tag_bytes + b" OK noop\r\n")
            elif command == "LIST":
                self.wfile.write(
                    b'* LIST (\\HasNoChildren) "/" "INBOX"\r\n'
                )
                self.wfile.write(tag_bytes + b" OK done\r\n")
            elif command in ("SELECT", "EXAMINE"):
                self.wfile.write(b"* 2 EXISTS\r\n* 0 RECENT\r\n")
                self.wfile.write(b"* OK [UIDVALIDITY 77] UIDs valid\r\n")
                self.wfile.write(tag_bytes + b" OK [READ-ONLY] done\r\n")
            elif command == "UID":
                subcommand, _, _subargs = args.partition(" ")
                if subcommand.upper() == "SEARCH":
                    self.wfile.write(b"* SEARCH 101 102\r\n")
                    self.wfile.write(tag_bytes + b" OK done\r\n")
                elif subcommand.upper() == "FETCH":
                    for sequence, (uid, raw, flags) in enumerate(
                        [(101, RAW_ONE, b"\\Seen"), (102, RAW_TWO, b"")],
                        start=1,
                    ):
                        header = (
                            b"* %d FETCH (UID %d FLAGS (%s) BODY[] {%d}\r\n"
                            % (sequence, uid, flags, len(raw))
                        )
                        self.wfile.write(header + raw + b")\r\n")
                    self.wfile.write(tag_bytes + b" OK done\r\n")
                else:
                    self.wfile.write(tag_bytes + b" BAD unknown UID command\r\n")
            elif command == "LOGOUT":
                self.wfile.write(b"* BYE bye\r\n" + tag_bytes + b" OK\r\n")
                return
            else:
                self.wfile.write(tag_bytes + b" BAD unknown command\r\n")


class WireTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.server = socketserver.ThreadingTCPServer(
            ("127.0.0.1", 0), MiniImapHandler
        )
        self.server.daemon_threads = True
        self.server.commands = []
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.client = ImapClient(
            "127.0.0.1",
            self.server.server_address[1],
            "me@example.com",
            "secret",
            use_ssl=False,
            timeout=5,
        )
        self.engine, self.store, self.backend, _ = build_engine(
            self.tmp, self.client
        )

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_full_cycle_over_real_imaplib(self):
        summary = self.engine.run_cycle(
            now=datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
        )
        self.assertEqual(summary.added, 2)
        self.assertFalse(any("BODY[]" in c and "PEEK" not in c for c in self.server.commands))
        self.assertTrue(any("BODY.PEEK[]" in c for c in self.server.commands))
        state = self.store.load_state()
        self.assertEqual(state["folders"]["INBOX"]["uidvalidity"], 77)

        restore = RestoreEngine(self.backend, self.tmp / "cache", jobs=2)
        manifest = restore.run(self.tmp / "out")
        self.assertEqual(manifest["folders"]["INBOX"]["messages"], 2)
        path = self.tmp / "out" / "INBOX.mbox"
        box = mailbox.mbox(str(path), create=False)
        try:
            self.assertEqual(len(box), 2)
            self.assertEqual(box.get(0).get("Subject"), "first")
            self.assertEqual(box.get(0).get("Status"), "R")
            self.assertIn(b">From the start", box.get(0).get_payload(decode=True))
        finally:
            box.close()


if __name__ == "__main__":
    unittest.main()
