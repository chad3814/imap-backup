"""Render stored messages as classic mbox files.

The store keeps exact RFC 822 bytes.  Everything mbox-specific (the ``From ``
separator line, Status/X-Status headers, CRLF normalisation and mboxrd
``>From`` escaping) is produced here, at restore time, from the event log and
the recorded flags.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from .events import Occurrence

_HEADER_SCAN_LIMIT = 65536
_STATUS_LINE = re.compile(rb"(?i)^(status|x-status)\s*:")
_FROM_ESCAPE = re.compile(rb"(?m)^(>*From )")


def _normalize_newlines(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _split_headers(raw: bytes) -> tuple[bytes, bytes]:
    separator = raw.find(b"\n\n")
    if separator < 0:
        return raw, b""
    return raw[:separator], raw[separator + 2 :]


def _strip_status_headers(headers: bytes) -> bytes:
    kept = [
        line
        for line in headers.split(b"\n")
        if not _STATUS_LINE.match(line)
    ]
    return b"\n".join(kept)


def _ascii_sender(head: bytes) -> str:
    match = re.search(rb"(?im)^from:\s*(.+)$", head[:_HEADER_SCAN_LIMIT])
    if not match:
        return "MAILER-DAEMON"
    _, address = parseaddr(match.group(1).decode("utf-8", "replace"))
    address = address.strip()
    if address and address.isascii():
        return address
    return "MAILER-DAEMON"


def _stamp(head: bytes, default: datetime) -> str:
    match = re.search(rb"(?im)^date:\s*(.+)$", head[:_HEADER_SCAN_LIMIT])
    if match:
        try:
            parsed = parsedate_to_datetime(
                match.group(1).decode("ascii", "replace")
            )
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return _asctime(parsed)
        except (TypeError, ValueError, OverflowError):
            pass
    return _asctime(default)


def _asctime(moment: datetime) -> str:
    # time.asctime renders the supplied struct_time verbatim, so this is stable
    # regardless of the container's local timezone.
    return time.asctime(moment.astimezone(UTC).timetuple())


def _status_headers(flags: Iterable[str]) -> bytes:
    present = set(flags)
    status = b"R" if r"\Seen" in present else b"O"
    extras = b""
    if r"\Answered" in present:
        extras += b"A"
    if r"\Flagged" in present:
        extras += b"F"
    if r"\Deleted" in present:
        extras += b"D"
    if r"\Draft" in present:
        extras += b"T"
    headers = b"Status: " + status + b"\n"
    if extras:
        headers += b"X-Status: " + extras + b"\n"
    return headers


def message_to_mbox(
    raw: bytes, flags: Iterable[str], default_date: datetime
) -> bytes:
    """Turn one stored message into a complete mbox entry."""
    normalized = _normalize_newlines(raw)
    headers, body = _split_headers(normalized)
    headers = _strip_status_headers(headers).rstrip(b"\n")
    from_line = (
        b"From "
        + _ascii_sender(headers).encode("ascii", "replace")
        + b" "
        + _stamp(headers, default_date).encode("ascii", "replace")
    )
    # mboxrd escaping: From -> >From, >From -> >>From, and so on.
    escaped_body = _FROM_ESCAPE.sub(rb">\1", body)
    if escaped_body and not escaped_body.endswith(b"\n"):
        escaped_body += b"\n"
    blob = (
        from_line
        + b"\n"
        + _status_headers(flags)
        + headers
        + b"\n\n"
        + escaped_body
    )
    if not blob.endswith(b"\n"):
        blob += b"\n"
    return blob


def render_mbox(
    occurrences: Iterable[Occurrence],
    blob_getter: Callable[[str], bytes],
    out_path: Path,
    default_date: datetime,
) -> tuple[int, int, str]:
    """Write an mbox file atomically.

    Returns ``(message_count, byte_count, sha256)``.  A missing blob raises
    ``KeyError`` rather than silently producing an incomplete archive.
    """
    digest = hashlib.sha256()
    count = 0
    total = 0
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tmp_path.open("wb") as handle:
            for occurrence in occurrences:
                blob = message_to_mbox(
                    blob_getter(occurrence.key), occurrence.flags, default_date
                )
                if count:
                    handle.write(b"\n")
                    digest.update(b"\n")
                    total += 1
                handle.write(blob)
                digest.update(blob)
                total += len(blob)
                count += 1
        if count == 0:
            tmp_path.unlink()
            return 0, 0, hashlib.sha256(b"").hexdigest()
        import os

        os.replace(tmp_path, out_path)
        return count, total, digest.hexdigest()
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


__all__ = ["message_to_mbox", "render_mbox"]
