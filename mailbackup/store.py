"""Local working store: content-addressed messages, numbered logs, and state."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .events import Record, read_jsonl, write_jsonl
from .util import (
    atomic_write_bytes,
    atomic_write_text,
    folder_dir_name,
    log_file_name,
    parse_log_file_name,
)

STATE_VERSION = 1


def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "last_cycle": "",
        "last_scan": "",
        "folders": {},
    }


class StoreError(Exception):
    pass


class LocalStore:
    """Everything that must survive a restart lives under ``root``.

    The directory is a cache and a write-ahead area, not a second source of
    truth: a fresh container can rebuild it from the remote.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.messages_dir = self.root / "messages"
        self.logs_dir = self.root / "mboxlogs"
        self.state_path = self.root / "state.json"
        self.dirty_blobs: set[str] = set()
        self.dirty_logs: set[Path] = set()
        # Blobs that are known to exist remotely but were deliberately not
        # downloaded.  They are never re-uploaded.
        self.remote_blob_keys: set[str] = set()

    # -- messages ---------------------------------------------------------

    def blob_path(self, key: str) -> Path:
        return self.messages_dir / key[:2] / f"{key}.eml"

    def has_blob(self, key: str) -> bool:
        return self.blob_path(key).exists() or key in self.remote_blob_keys

    def read_blob(self, key: str) -> bytes:
        return self.blob_path(key).read_bytes()

    def put_blob(self, raw: bytes) -> tuple[str, bool]:
        if not raw:
            raise StoreError("refusing to store an empty message")
        import hashlib

        key = hashlib.sha256(raw).hexdigest()
        if self.has_blob(key):
            return key, False
        atomic_write_bytes(self.blob_path(key), raw)
        self.dirty_blobs.add(key)
        return key, True

    def iter_blob_keys(self):
        if not self.messages_dir.exists():
            return
        for path in self.messages_dir.glob("*/*.eml"):
            yield path.stem

    # -- numbered event logs ---------------------------------------------

    def log_prefix(self, folder: str) -> str:
        return folder_dir_name(folder)

    def log_path(self, folder: str, seq: int) -> Path:
        return self.logs_dir / log_file_name(folder, seq)

    def log_seq(self, path: Path) -> int:
        parsed = parse_log_file_name(path.name)
        if parsed is None:
            raise StoreError(f"{path} is not a numbered mbox log")
        return parsed[1]

    def folder_log_paths(self, folder: str) -> list[Path]:
        if not self.logs_dir.exists():
            return []
        found: list[tuple[int, Path]] = []
        for path in self.logs_dir.iterdir():
            if not path.is_file() or not path.name.endswith(".jsonl"):
                continue
            parsed = parse_log_file_name(path.name)
            if parsed is not None and parsed[0] == folder:
                found.append((parsed[1], path))
        return [path for _, path in sorted(found)]

    def latest_log_path(self, folder: str) -> Path | None:
        paths = self.folder_log_paths(folder)
        return paths[-1] if paths else None

    def next_log_seq(self, folder: str) -> int:
        paths = self.folder_log_paths(folder)
        if not paths:
            return 0
        return self.log_seq(paths[-1]) + 1

    def write_log(
        self, folder: str, seq: int, records: Iterable[Record]
    ) -> Path:
        path = self.log_path(folder, seq)
        count = write_jsonl(path, records)
        if count:
            self.dirty_logs.add(path)
        return path

    def read_log(self, path: Path) -> list[Record]:
        try:
            return read_jsonl(path)
        except ValueError as exc:
            raise StoreError(str(exc)) from exc

    def iter_log_paths(self):
        if not self.logs_dir.exists():
            return
        yield from sorted(self.logs_dir.glob("*.jsonl"))

    # -- state ------------------------------------------------------------

    def load_state(self) -> dict:
        if not self.state_path.exists():
            return empty_state()
        import json

        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StoreError(f"cannot read {self.state_path}: {exc}") from exc
        if not isinstance(state, dict) or not isinstance(state.get("folders"), dict):
            raise StoreError(f"{self.state_path} is not a valid state file")
        state.setdefault("version", STATE_VERSION)
        state.setdefault("last_cycle", "")
        state.setdefault("last_scan", "")
        return state

    def save_state(self, state: dict) -> None:
        import json

        atomic_write_text(
            self.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n"
        )

    # -- remote key mapping ----------------------------------------------

    def remote_key(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def remote_blob_key(self, key: str) -> str:
        return f"messages/{key[:2]}/{key}.eml"

    def mark_blob_clean(self, key: str) -> None:
        self.dirty_blobs.discard(key)

    def mark_log_clean(self, path: Path) -> None:
        self.dirty_logs.discard(path)

    def local_remote_keys(self) -> set[str]:
        keys: set[str] = set()
        for key in self.iter_blob_keys():
            keys.add(self.remote_blob_key(key))
        for path in self.iter_log_paths():
            keys.add(self.remote_key(path))
        if self.state_path.exists():
            keys.add("state.json")
        return keys

    def reconcile(self, remote_keys: set[str]) -> None:
        """Queue local files that the remote does not have yet."""
        for key in self.iter_blob_keys():
            if self.remote_blob_key(key) not in remote_keys:
                self.dirty_blobs.add(key)
        for path in self.iter_log_paths():
            if self.remote_key(path) not in remote_keys:
                self.dirty_logs.add(path)

    def adopt_remote_state(self, remote_state: dict) -> None:
        """Use the remote state when it is ahead of the local one."""
        local = self.load_state()
        if remote_state.get("last_cycle", "") > local.get("last_cycle", ""):
            self.save_state(remote_state)
