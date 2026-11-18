"""Append-only event log for folder membership.

Each folder is a numbered sequence of JSONL files (``INBOX-00000000.jsonl``,
``INBOX-00000001.jsonl``, ...).  **Every file starts with a ``reset`` record
that lists the complete folder contents at that point**, followed by ``add`` and
``rm`` records for later changes.  A file is therefore self-contained: a mirror
restore only needs the newest file whose reset is at or before the target time.

Message bytes are never part of the log.  They live in the content-addressed
store and are referenced by SHA-256.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

ADD = "add"
REMOVE = "rm"
RESET = "reset"
_TYPES = {ADD, REMOVE, RESET}


@dataclass(frozen=True)
class Occurrence:
    """One message as it appeared in one folder at one point in its history."""

    uid: int
    key: str
    flags: tuple[str, ...] = ()
    message_id: str = ""
    size: int = 0

    def to_item(self) -> list:
        return [self.uid, self.key, list(self.flags), self.message_id, self.size]

    @classmethod
    def from_item(cls, item: Sequence) -> Occurrence:
        uid, key, flags, message_id, size = (list(item) + [0, "", [], "", 0])[:5]
        return cls(
            uid=int(uid),
            key=str(key),
            flags=tuple(str(flag) for flag in (flags or [])),
            message_id=str(message_id or ""),
            size=int(size or 0),
        )


@dataclass(frozen=True)
class Record:
    type: str
    cycle: str
    folder: str
    uid: int = 0
    key: str = ""
    flags: tuple[str, ...] = ()
    message_id: str = ""
    size: int = 0
    uidvalidity: int = 0
    items: tuple[Occurrence, ...] = field(default_factory=tuple)

    # -- constructors -----------------------------------------------------

    @classmethod
    def add(cls, cycle: str, folder: str, occurrence: Occurrence) -> Record:
        return cls(
            type=ADD,
            cycle=cycle,
            folder=folder,
            uid=occurrence.uid,
            key=occurrence.key,
            flags=occurrence.flags,
            message_id=occurrence.message_id,
            size=occurrence.size,
        )

    @classmethod
    def remove(cls, cycle: str, folder: str, uid: int, key: str) -> Record:
        return cls(type=REMOVE, cycle=cycle, folder=folder, uid=uid, key=key)

    @classmethod
    def reset(
        cls,
        cycle: str,
        folder: str,
        uidvalidity: int,
        occurrences: Iterable[Occurrence],
    ) -> Record:
        return cls(
            type=RESET,
            cycle=cycle,
            folder=folder,
            uidvalidity=uidvalidity,
            items=tuple(occurrences),
        )

    # -- serialisation ----------------------------------------------------

    def to_json(self) -> str:
        payload: dict = {
            "v": SCHEMA_VERSION,
            "t": self.type,
            "c": self.cycle,
            "f": self.folder,
        }
        if self.type in (ADD, REMOVE):
            payload["u"] = self.uid
            payload["k"] = self.key
        if self.type == ADD:
            if self.flags:
                payload["fl"] = list(self.flags)
            if self.message_id:
                payload["m"] = self.message_id
            if self.size:
                payload["s"] = self.size
        if self.type == RESET:
            payload["uv"] = self.uidvalidity
            payload["items"] = [item.to_item() for item in self.items]
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, obj: dict) -> Record:
        version = obj.get("v")
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported event schema version {version!r}")
        kind = obj.get("t")
        if kind not in _TYPES:
            raise ValueError(f"unknown event type {kind!r}")
        cycle = str(obj.get("c") or "")
        folder = str(obj.get("f") or "")
        if not cycle or not folder:
            raise ValueError("event is missing cycle or folder")
        items = tuple(
            Occurrence.from_item(item) for item in (obj.get("items") or [])
        )
        return cls(
            type=kind,
            cycle=cycle,
            folder=folder,
            uid=int(obj.get("u") or 0),
            key=str(obj.get("k") or ""),
            flags=tuple(str(flag) for flag in (obj.get("fl") or [])),
            message_id=str(obj.get("m") or ""),
            size=int(obj.get("s") or 0),
            uidvalidity=int(obj.get("uv") or 0),
            items=items,
        )


def replay(
    records: Iterable[Record],
    cutoff: str | None = None,
    keep_removed: bool = False,
) -> list[Occurrence]:
    """Fold records into folder contents.

    The first record must be a reset (callers validate this).  ``keep_removed``
    ignores removals and unions every message ever listed in a reset or added:
    that is the per-folder archive view.
    """
    ordered = sorted(records, key=lambda record: record.cycle)
    current: dict = {}
    if keep_removed:
        for record in ordered:
            if cutoff is not None and record.cycle > cutoff:
                continue
            if record.type == RESET:
                for item in record.items:
                    current.setdefault(item.key, item)
            elif record.type == ADD:
                current.setdefault(
                    record.key,
                    Occurrence(
                        uid=record.uid,
                        key=record.key,
                        flags=record.flags,
                        message_id=record.message_id,
                        size=record.size,
                    ),
                )
        return list(current.values())

    for record in ordered:
        if cutoff is not None and record.cycle > cutoff:
            continue
        if record.type == RESET:
            current = {item.uid: item for item in record.items}
        elif record.type == ADD:
            current[record.uid] = Occurrence(
                uid=record.uid,
                key=record.key,
                flags=record.flags,
                message_id=record.message_id,
                size=record.size,
            )
        elif record.type == REMOVE:
            current.pop(record.uid, None)
    return list(current.values())


def write_jsonl(path: Path, records: Iterable[Record]) -> int:
    """Write records atomically; returns the number of lines."""
    from .util import atomic_write_bytes

    lines = [record.to_json() + "\n" for record in records]
    atomic_write_bytes(path, "".join(lines).encode("utf-8"))
    return len(lines)


def read_jsonl(path: Path) -> list[Record]:
    records: list[Record] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(Record.from_json(json.loads(line)))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc
    return records


def parse_jsonl_bytes(data: bytes, origin: str = "<memory>") -> list[Record]:
    records: list[Record] = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(Record.from_json(json.loads(line)))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{origin}:{number}: {exc}") from exc
    return records
