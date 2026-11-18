"""Manage one folder's numbered, self-contained JSONL log files.

A log file has this shape::

    {"t":"reset", ... ,"items":[...]}     # baseline: the whole folder
    {"t":"add", ...}
    {"t":"rm", ...}
    ...

The reset is always the first line.  Files hold at most ``max_ops`` records in
total (the reset baseline plus add/rm operations); when the cap is reached (or a
snapshot interval elapses) the current file is closed and the next one starts
with a fresh reset baseline.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .events import ADD, REMOVE, RESET, Occurrence, Record, replay
from .store import LocalStore
from .util import parse_when


class LogFileError(Exception):
    pass


class ActiveLog:
    def __init__(self, store: LocalStore, folder: str, max_ops: int):
        self.store = store
        self.folder = folder
        # The reset baseline is the first record, so a file of max_ops records
        # holds at most max_ops - 1 add/rm operations.
        self.max_ops = max(2, max_ops)
        self.seq = 0
        self.reset: Record | None = None
        self.ops: list[Record] = []
        self.state: dict[int, Occurrence] = {}
        self.dirty = False
        self._load()

    # -- properties -------------------------------------------------------

    @property
    def uidvalidity(self) -> int:
        return self.reset.uidvalidity if self.reset else 0

    @property
    def file_start(self) -> str:
        return self.reset.cycle if self.reset else ""

    @property
    def ops_count(self) -> int:
        return len(self.ops)

    @property
    def path(self) -> Path | None:
        return self.store.log_path(self.folder, self.seq) if self.reset else None

    # -- loading ----------------------------------------------------------

    def _load(self) -> None:
        paths = self.store.folder_log_paths(self.folder)
        if not paths:
            return
        path = paths[-1]
        self.seq = self.store.log_seq(path)
        records = self.store.read_log(path)
        if not records:
            raise LogFileError(f"{path} is empty; a reset baseline is required")
        if records[0].type != RESET:
            raise LogFileError(f"{path} does not start with a reset baseline")
        if any(record.type == RESET for record in records[1:]):
            raise LogFileError(f"{path} has a reset after its first line")
        self.reset = records[0]
        self.ops = records[1:]
        self.state = {
            occurrence.uid: occurrence for occurrence in replay(records)
        }

    # -- mutations --------------------------------------------------------

    def start(
        self, cycle: str, uidvalidity: int, occurrences: Iterable[Occurrence]
    ) -> None:
        """Create the first file for a folder (seq 0) with a reset baseline."""
        items = list(occurrences)
        self.reset = Record.reset(cycle, self.folder, uidvalidity, items)
        self.ops = []
        self.state = {occurrence.uid: occurrence for occurrence in items}
        self.dirty = True

    def reset_to(
        self, cycle: str, uidvalidity: int, occurrences: Iterable[Occurrence]
    ) -> None:
        """Start a new file with a new baseline (used for UIDVALIDITY changes)."""
        if self.reset is None:
            self.start(cycle, uidvalidity, occurrences)
            return
        self.save()
        self.seq += 1
        items = list(occurrences)
        self.reset = Record.reset(cycle, self.folder, uidvalidity, items)
        self.ops = []
        self.state = {occurrence.uid: occurrence for occurrence in items}
        self.dirty = True

    def append(self, record: Record, cycle: str, uidvalidity: int) -> None:
        if record.type not in (ADD, REMOVE):
            raise LogFileError(f"cannot append a {record.type!r} operation")
        if self.reset is None:
            self.start(cycle, uidvalidity, [])
        if len(self.ops) + 1 >= self.max_ops:
            self.roll(cycle, uidvalidity)
        self.ops.append(record)
        self._apply(record)
        self.dirty = True

    def roll(self, cycle: str, uidvalidity: int) -> None:
        """Close the current file and start the next with a fresh baseline."""
        self.save()
        self.seq += 1
        self.reset = Record.reset(
            cycle, self.folder, uidvalidity, list(self.state.values())
        )
        self.ops = []
        self.dirty = True

    def maybe_roll_time(
        self, cycle: str, now, interval_seconds: int, uidvalidity: int
    ) -> bool:
        """Close the file early to produce a snapshot (e.g. weekly)."""
        if self.reset is None:
            return False
        try:
            started = parse_when(self.reset.cycle)
        except ValueError:
            return False
        if (now - started).total_seconds() < interval_seconds:
            return False
        self.roll(cycle, uidvalidity)
        return True

    # -- persistence ------------------------------------------------------

    def save(self) -> Path | None:
        if not self.dirty or self.reset is None:
            return None
        path = self.store.write_log(
            self.folder, self.seq, [self.reset, *self.ops]
        )
        self.dirty = False
        return path

    def _apply(self, record: Record) -> None:
        if record.type == ADD:
            self.state[record.uid] = Occurrence(
                uid=record.uid,
                key=record.key,
                flags=record.flags,
                message_id=record.message_id,
                size=record.size,
            )
        elif record.type == REMOVE:
            self.state.pop(record.uid, None)
