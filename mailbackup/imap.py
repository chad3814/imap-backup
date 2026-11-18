"""Thin, testable wrapper around imaplib.

Deliberately close to the exporter that was already validated against a real
Hover account: folders are opened read-only and bodies are fetched with
``BODY.PEEK[]`` so backing up never marks mail as read.
"""

from __future__ import annotations

import base64
import email.policy
import imaplib
import re
import ssl
from dataclasses import dataclass
from email.parser import BytesHeaderParser

LIST_RE = re.compile(
    rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|"(?:[^"\\]|\\.)*")\s+(?P<name>.+)$'
)
FETCH_UID_RE = re.compile(rb"\bUID\s+(\d+)")
FETCH_FLAGS_RE = re.compile(rb"\bFLAGS\s+\(([^)]*)\)")
FETCH_BODY_RE = re.compile(rb"\bBODY\[\]")


class ImapError(Exception):
    """An IMAP operation failed in a way the caller should report."""


def decode_imap_utf7(name: str) -> str:
    """Decode an IMAP mailbox name from modified UTF-7 (RFC 3501 5.1.3)."""
    out: list[str] = []
    i = 0
    while i < len(name):
        char = name[i]
        if char != "&":
            out.append(char)
            i += 1
            continue
        end = name.find("-", i + 1)
        if end == -1:
            out.append(char)
            i += 1
            continue
        chunk = name[i + 1 : end]
        if chunk == "":
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                raw = base64.b64decode(b64)
                out.append(raw.decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(name[i : end + 1])
        i = end + 1
    return "".join(out)


def _unquote_imap_name(raw: bytes) -> str:
    if raw.startswith(b'"') and raw.endswith(b'"') and len(raw) >= 2:
        raw = re.sub(rb"\\(.)", rb"\1", raw[1:-1])
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


@dataclass(frozen=True)
class Folder:
    name: str  # Human-readable, decoded.
    select_name: str  # Unquoted mailbox name, still in modified UTF-7.
    flags: frozenset[str]

    @property
    def selectable(self) -> bool:
        return not ({"Noselect", "NonExistent"} & self.flags)


def parse_list_response(lines) -> list[Folder]:
    folders: list[Folder] = []
    for line in lines or []:
        if isinstance(line, tuple):
            line = line[0]
        if not isinstance(line, (bytes, bytearray)):
            continue
        match = LIST_RE.match(bytes(line).rstrip(b"\r\n"))
        if not match:
            continue
        flags = frozenset(
            token.decode("ascii", "replace").lstrip("\\")
            for token in match.group("flags").split()
        )
        select_name = _unquote_imap_name(match.group("name"))
        folders.append(
            Folder(
                name=decode_imap_utf7(select_name),
                select_name=select_name,
                flags=flags,
            )
        )
    return folders


def imap_mailbox_arg(conn: imaplib.IMAP4, name: str) -> str:
    """Return a mailbox name SELECT can accept across imaplib versions."""
    if hasattr(conn, "_astring"):
        return name
    needs_quotes = (
        any(char in name for char in ' ()[]{%*"\\')
        or not name.isascii()
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in name)
    )
    if needs_quotes:
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return name


def get_uidvalidity(conn: imaplib.IMAP4) -> int:
    typ, data = conn.response("UIDVALIDITY")
    if typ == "UIDVALIDITY" and data and data[0]:
        try:
            return int(data[0])
        except (TypeError, ValueError):
            pass
    return 0


@dataclass(frozen=True)
class FetchedMessage:
    uid: int
    flags: frozenset[str]
    raw: bytes


def parse_fetch_response(items) -> list[FetchedMessage]:
    messages: list[FetchedMessage] = []
    for item in items or []:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        meta, raw = item
        if not isinstance(meta, (bytes, bytearray)) or not FETCH_BODY_RE.search(meta):
            continue
        uid_match = FETCH_UID_RE.search(meta)
        if not uid_match:
            continue
        flags_match = FETCH_FLAGS_RE.search(meta)
        flags = frozenset()
        if flags_match:
            flags = frozenset(
                token.decode("ascii", "replace")
                for token in flags_match.group(1).split()
            )
        messages.append(
            FetchedMessage(
                uid=int(uid_match.group(1)),
                flags=flags,
                raw=bytes(raw or b""),
            )
        )
    messages.sort(key=lambda message: message.uid)
    return messages


_MESSAGE_ID_RE = re.compile(rb"(?im)^message-id:\s*(.+?)\s*$")


def message_id_of(raw: bytes) -> str:
    """Best-effort Message-ID, used only as searchable metadata."""
    try:
        headers = BytesHeaderParser(policy=email.policy.compat32).parsebytes(
            raw[:65536]
        )
        value = headers.get("Message-ID") or headers.get("Message-Id")
        if value:
            return " ".join(str(value).split())[:998]
    except Exception:  # noqa: BLE001,S110  # pragma: no cover - never stop a backup
        pass
    match = _MESSAGE_ID_RE.search(raw[:65536])
    if match:
        return " ".join(match.group(1).decode("utf-8", "replace").split())[:998]
    return ""


class ImapClient:
    """A connection that can be re-established after a dropped socket."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        use_ssl: bool = True,
        timeout: int = 60,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.conn: imaplib.IMAP4 | None = None
        self.last_exists: int | None = None

    def connect(self) -> None:
        if self.use_ssl:
            self.conn = imaplib.IMAP4_SSL(
                self.host,
                self.port,
                ssl_context=ssl.create_default_context(),
                timeout=self.timeout,
            )
        else:
            self.conn = imaplib.IMAP4(
                self.host, self.port, timeout=self.timeout
            )
        try:
            self.conn.login(self.user, self.password)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:  # noqa: BLE001,S110  # logout is best-effort
                pass
            self.conn = None

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def list_folders(self) -> list[Folder]:
        assert self.conn is not None
        typ, data = self.conn.list()
        if typ != "OK":
            raise ImapError(f"LIST failed: {data!r}")
        return parse_list_response(data)

    def select(self, folder: Folder) -> None:
        assert self.conn is not None
        self.last_exists = None
        name = imap_mailbox_arg(self.conn, folder.select_name)
        typ, data = self.conn.select(name, readonly=True)
        if typ != "OK":
            raise ImapError(f"cannot open folder {folder.name!r}: {data!r}")
        if data:
            try:
                self.last_exists = int(data[0])
            except (TypeError, ValueError):
                self.last_exists = None

    def uidvalidity(self) -> int:
        assert self.conn is not None
        return get_uidvalidity(self.conn)

    def search_all_uids(self) -> list[int]:
        assert self.conn is not None
        typ, data = self.conn.uid("search", None, "ALL")
        if typ != "OK":
            raise ImapError(f"UID SEARCH failed: {data!r}")
        raw = data[0] if data else b""
        if not raw:
            return []
        return sorted(int(value) for value in raw.split())

    def fetch(self, uids: list[int]) -> list[FetchedMessage]:
        assert self.conn is not None
        if not uids:
            return []
        typ, data = self.conn.uid(
            "fetch",
            ",".join(str(uid) for uid in uids),
            "(UID FLAGS BODY.PEEK[])",
        )
        if typ != "OK":
            raise ImapError(f"UID FETCH failed: {data!r}")
        return parse_fetch_response(data)
