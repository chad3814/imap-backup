"""Small helpers shared by the backup, restore and storage layers."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime, time
from pathlib import Path
from urllib.parse import quote, unquote

CYCLE_FORMAT = "%Y%m%dT%H%M%S.%fZ"


class ConfigError(Exception):
    """A configuration problem the operator can fix."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def cycle_name(moment: datetime) -> str:
    """A lexicographically sortable UTC cycle id, also safe as a filename."""
    return moment.astimezone(UTC).strftime(CYCLE_FORMAT)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_when(value: str) -> datetime:
    """Parse ``YYYY-MM-DD``, ISO-8601, or an internal cycle id into UTC."""
    raw = value.strip()
    if not raw:
        raise ValueError("empty timestamp")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        day = datetime.strptime(raw, "%Y-%m-%d")
        return datetime.combine(day.date(), time(23, 59, 59, 999999), tzinfo=UTC)
    if re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z", raw):
        return datetime.strptime(raw, CYCLE_FORMAT).replace(tzinfo=UTC)
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # A timestamp without a fractional part means "the whole second" (or day,
    # handled above), not "the first microsecond of it".  Backup cycles have
    # microsecond ids, so this keeps --at inclusive at the stated precision.
    if not re.search(r"\.\d+", raw):
        parsed = parsed.replace(microsecond=999999)
    return parsed.astimezone(UTC)


def as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def folder_dir_name(folder: str) -> str:
    """A reversible, filesystem- and S3-safe directory name for a folder."""
    if not folder:
        raise ValueError("empty folder name")
    encoded = quote(folder, safe="")
    # A folder literally named "." or ".." must not resolve to a parent
    # directory.  Encoding the leading dot keeps the name reversible and safe.
    if encoded.startswith("."):
        encoded = "%2E" + encoded[1:]
    return encoded


def folder_from_dir_name(name: str) -> str:
    return unquote(name)


def safe_stem(name: str, used: set[str]) -> str:
    """Turn an IMAP folder name into a unique mbox filename stem."""
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    stem = stem[:80] or "mailbox"
    candidate = stem
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{counter}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))
    # State and manifests are not secret; make them readable by whoever
    # mounts the output/data directory.  Message blobs and event logs stay 0600.
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def human_bytes(value: int) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return f"{int(value)} B"


LOG_NAME_RE = re.compile(r"^(.+)-(\d{8})\.jsonl$")


def log_file_name(folder: str, seq: int) -> str:
    """INBOX-00000000.jsonl (the folder part is URL-quoted)."""
    return f"{folder_dir_name(folder)}-{seq:08d}.jsonl"


def parse_log_file_name(name: str) -> tuple[str, int] | None:
    """Split ``<encoded folder>-<8 digit sequence>.jsonl``.

    Parsing from the right means folder names that themselves end in
    ``-00000000`` still round-trip.
    """
    match = LOG_NAME_RE.fullmatch(name)
    if not match:
        return None
    return unquote(match.group(1)), int(match.group(2))
