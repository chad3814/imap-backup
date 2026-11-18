"""Shared helpers for the test suite."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mailbackup.backend import DirBackend, make_backend
from mailbackup.backup import BackupEngine
from mailbackup.config import Config
from mailbackup.imap import FetchedMessage, Folder
from mailbackup.store import LocalStore


@dataclass
class Box:
    uidvalidity: int = 1
    messages: dict[int, tuple[bytes, frozenset[str]]] = field(default_factory=dict)
    flags: frozenset[str] = frozenset()


class FakeConn:
    def __init__(self) -> None:
        self.noop_calls = 0

    def noop(self):
        self.noop_calls += 1
        return "OK", [b""]


class FakeImap:
    """In-memory stand-in for ImapClient, with UID semantics."""

    def __init__(self) -> None:
        self.conn = FakeConn()
        self.boxes: dict[str, Box] = {}
        self.selected = ""
        self.fetched: list[int] = []
        self.reconnects = 0
        self.selected_readonly: list[bool] = []
        self.last_exists: int | None = None

    def add_folder(
        self, name: str, uidvalidity: int = 1, flags: frozenset[str] = frozenset()
    ) -> None:
        self.boxes[name] = Box(uidvalidity=uidvalidity, flags=flags)

    def add(
        self,
        folder: str,
        uid: int,
        raw: bytes,
        flags: frozenset[str] = frozenset(),
    ) -> None:
        self.boxes[folder].messages[uid] = (raw, flags)

    def move(
        self,
        source: str,
        uid: int,
        target: str,
        new_uid: int,
        flags: frozenset[str] | None = None,
    ) -> None:
        raw, old_flags = self.boxes[source].messages.pop(uid)
        self.boxes[target].messages[new_uid] = (
            raw,
            old_flags if flags is None else flags,
        )

    def delete(self, folder: str, uid: int) -> None:
        self.boxes[folder].messages.pop(uid)

    def set_uidvalidity(self, folder: str, value: int) -> None:
        self.boxes[folder].uidvalidity = value

    # -- ImapClient interface --------------------------------------------

    def list_folders(self) -> list[Folder]:
        return [
            Folder(name=name, select_name=name, flags=box.flags)
            for name, box in self.boxes.items()
        ]

    def select(self, folder: Folder) -> None:
        self.selected = folder.name
        self.selected_readonly.append(True)
        self.last_exists = len(self.boxes[folder.name].messages)

    def uidvalidity(self) -> int:
        return self.boxes[self.selected].uidvalidity

    def search_all_uids(self) -> list[int]:
        return sorted(self.boxes[self.selected].messages)

    def fetch(self, uids: list[int]) -> list[FetchedMessage]:
        self.fetched.extend(uids)
        result = []
        for uid in uids:
            if uid not in self.boxes[self.selected].messages:
                continue
            raw, flags = self.boxes[self.selected].messages[uid]
            result.append(FetchedMessage(uid=uid, flags=flags, raw=raw))
        return result

    def reconnect(self) -> None:
        self.reconnects += 1
        self.conn = FakeConn()

    def close(self) -> None:
        self.conn = None


def make_raw(
    subject: str,
    body: str = "hello\r\n",
    msgid: str | None = None,
    sender: str = "alice@example.com",
    date: str = "Tue, 18 Nov 2026 09:00:00 -0600",
    extra_headers: str = "",
) -> bytes:
    headers = (
        f"From: {sender}\r\n"
        f"Date: {date}\r\n"
        f"Subject: {subject}\r\n"
    )
    if msgid:
        headers += f"Message-ID: <{msgid}>\r\n"
    if extra_headers:
        headers += extra_headers
    return (headers + "\r\n" + body).encode()


def make_config(tmp: Path, **overrides) -> Config:
    values = {
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_user": "user@example.com",
        "imap_password": "secret",
        "imap_ssl": False,
        "data_dir": tmp / "data",
        "backend": "dir",
        "backup_dir": tmp / "remote",
        "interval_seconds": 300,
        "max_ops_per_file": 5000,
        "snapshot_every_seconds": 86400,
    }
    values.update(overrides)
    return Config(**values)


def build_engine(
    tmp: Path, client: FakeImap, **overrides
) -> tuple[BackupEngine, LocalStore, object, Config]:
    config = make_config(tmp, **overrides)
    store = LocalStore(config.data_dir)
    backend = (
        DirBackend(config.backup_dir)
        if config.backend == "dir"
        else make_backend(config)
    )
    engine = BackupEngine(config, store, client, backend)
    return engine, store, backend, config
