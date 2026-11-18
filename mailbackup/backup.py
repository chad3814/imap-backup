"""One backup cycle: reconcile every folder and persist the deltas."""

from __future__ import annotations

import imaplib
import json
import logging
import re
import ssl
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

from .backend import Backend, NullBackend
from .config import Config
from .events import Occurrence, Record
from .imap import FetchedMessage, Folder, ImapClient, ImapError, message_id_of
from .logfile import ActiveLog
from .store import LocalStore
from .util import cycle_name, human_bytes, iso, utcnow

log = logging.getLogger("mailbackup.backup")

EXCLUDED_FLAGS = {"Trash", "Junk", "All"}
EXCLUDED_NAMES = {
    "trash",
    "deleted items",
    "deleted messages",
    "junk",
    "spam",
    "junk e-mail",
    "bulk mail",
    "all mail",
    "starred",
    "important",
}

_BLOB_KEY_RE = re.compile(r"^messages/([0-9a-f]{2})/([0-9a-f]{64})\.eml$")


class BackupError(Exception):
    pass


@dataclass
class FolderSummary:
    name: str
    added: int = 0
    removed: int = 0
    reset: bool = False
    baseline: bool = False
    snapshot: bool = False
    vanished: int = 0
    new_blobs: int = 0
    new_bytes: int = 0
    error: str | None = None


@dataclass
class CycleSummary:
    cycle: str
    folders: list[FolderSummary] = field(default_factory=list)
    uploaded_blobs: int = 0
    uploaded_logs: int = 0
    uploaded_bytes: int = 0
    dry_run: bool = False

    @property
    def added(self) -> int:
        return sum(folder.added for folder in self.folders)

    @property
    def removed(self) -> int:
        return sum(folder.removed for folder in self.folders)

    @property
    def errors(self) -> int:
        return sum(1 for folder in self.folders if folder.error)

    def line(self) -> str:
        prefix = "dry run" if self.dry_run else "cycle"
        return (
            f"{prefix} {self.cycle}: +{self.added} -{self.removed} "
            f"across {len(self.folders)} folder(s); "
            f"{self.uploaded_blobs} blob(s), {self.uploaded_logs} log(s), "
            f"{human_bytes(self.uploaded_bytes)} uploaded"
            + (f"; {self.errors} error(s)" if self.errors else "")
        )


class BackupEngine:
    def __init__(
        self,
        config: Config,
        store: LocalStore,
        client: ImapClient,
        backend: Backend,
        logger: logging.Logger | None = None,
    ):
        self.config = config
        self.store = store
        self.client = client
        self.backend = backend
        self.log = logger or log
        self.remote_keys: set[str] = set()
        self._bootstrapped = False

    # -- public API -------------------------------------------------------

    def bootstrap(self) -> None:
        """Reconcile local and remote state before the first cycle."""
        if self._bootstrapped:
            return
        remote_keys = self.backend.list_keys("")
        self.remote_keys = remote_keys
        self.log.info(
            "remote contains %d object(s) under %s",
            len(remote_keys),
            self.backend,
        )

        if "state.json" in remote_keys:
            try:
                remote_state = json.loads(self.backend.get_bytes("state.json"))
            except (ValueError, OSError) as exc:
                raise BackupError(f"cannot read remote state.json: {exc}") from exc
            if not isinstance(remote_state, dict) or not isinstance(
                remote_state.get("folders"), dict
            ):
                raise BackupError("remote state.json is not a valid state file")
            if not self.store.state_path.exists():
                self.store.save_state(remote_state)
                self.log.info(
                    "restored state from remote (last_cycle=%s)",
                    remote_state.get("last_cycle") or "never",
                )
            else:
                self.store.adopt_remote_state(remote_state)

        missing_logs = [
            key
            for key in remote_keys
            if key.startswith("mboxlogs/")
            and key.endswith(".jsonl")
            and not (self.store.root / key).exists()
        ]
        if missing_logs:
            self.log.info("downloading %d log file(s) from remote", len(missing_logs))
            self._download_many(missing_logs)

        for key in remote_keys:
            match = _BLOB_KEY_RE.match(key)
            if match:
                self.store.remote_blob_keys.add(match.group(2))

        self.store.reconcile(remote_keys)
        # The active log file is rewritten as operations are appended, so a
        # remote key may hold an older, shorter version (e.g. the process died
        # between the local write and the upload).  Appends only grow a file,
        # so a size mismatch reliably means the local copy is newer.
        for path in self.store.iter_log_paths():
            key = self.store.remote_key(path)
            try:
                remote_size = self.backend.size(key)
            except Exception as exc:  # noqa: BLE001  # treat as missing/outdated
                self.log.warning("cannot stat remote %s: %s", key, exc)
                remote_size = None
            if remote_size != path.stat().st_size:
                self.store.dirty_logs.add(path)
        if self.store.dirty_blobs or self.store.dirty_logs:
            self.log.info(
                "queued %d blob(s) and %d log file(s) for upload",
                len(self.store.dirty_blobs),
                len(self.store.dirty_logs),
            )
        self._bootstrapped = True

    def run_cycle(
        self, now: datetime | None = None, dry_run: bool | None = None
    ) -> CycleSummary:
        dry_run = self.config.dry_run if dry_run is None else dry_run
        self.bootstrap()
        now = now or utcnow()
        cycle = cycle_name(now)
        state = self.store.load_state()
        summary = CycleSummary(cycle=cycle, dry_run=dry_run)

        self._ensure_connection()
        folders = self.client.list_folders()
        chosen = self.choose_folders(folders)
        if not chosen:
            raise BackupError("no folders selected")
        self.log.info(
            "cycle %s: checking %d folder(s)%s",
            cycle,
            len(chosen),
            " (dry run)" if dry_run else "",
        )

        for folder in chosen:
            result = FolderSummary(name=folder.name)
            try:
                self._process_folder(folder, state, cycle, now, dry_run, result)
            except Exception as exc:  # one bad folder must not kill the cycle
                result.error = f"{type(exc).__name__}: {exc}"
                self.log.error("[%s] failed: %s", folder.name, result.error)
                self.log.debug("folder failure detail", exc_info=True)
            summary.folders.append(result)
            self.log.info(
                "[%s] +%d -%d%s%s%s",
                folder.name,
                result.added,
                result.removed,
                " baseline" if result.baseline else "",
                " reset" if result.reset else "",
                " snapshot" if result.snapshot else "",
            )

        if not dry_run:
            succeeded = any(result.error is None for result in summary.folders)
            if succeeded:
                state["last_scan"] = iso(now)
            self.store.save_state(state)
            try:
                self._flush(summary)
                if succeeded:
                    state["last_cycle"] = cycle
                    self.store.save_state(state)
                self._upload_state(state)
            except Exception as exc:
                self.log.error("upload failed; will retry next cycle: %s", exc)
                self.log.debug("upload failure detail", exc_info=True)
                raise

        self.log.info(summary.line())
        return summary

    # -- folder selection -------------------------------------------------

    def choose_folders(self, folders: list[Folder]) -> list[Folder]:
        if self.config.imap_folders:
            by_name = {folder.name.lower(): folder for folder in folders}
            chosen: list[Folder] = []
            for wanted in self.config.imap_folders:
                folder = by_name.get(wanted.lower())
                if folder is None:
                    available = ", ".join(sorted(f.name for f in folders)) or "none"
                    raise BackupError(
                        f"folder {wanted!r} not found (available: {available})"
                    )
                if not folder.selectable:
                    raise BackupError(f"folder {wanted!r} cannot be selected")
                if folder not in chosen:
                    chosen.append(folder)
            return chosen

        if self.config.imap_all_folders:
            return [folder for folder in folders if folder.selectable]

        return [
            folder
            for folder in folders
            if folder.selectable
            and folder.name.lower() not in EXCLUDED_NAMES
            and not (folder.flags & EXCLUDED_FLAGS)
        ]

    # -- per-folder reconciliation ---------------------------------------

    def _process_folder(
        self,
        folder: Folder,
        state: dict,
        cycle: str,
        now: datetime,
        dry_run: bool,
        summary: FolderSummary,
    ) -> None:
        self._select(folder)
        uidvalidity = self.client.uidvalidity()
        current = self.client.search_all_uids()
        current_set = set(current)
        exists = getattr(self.client, "last_exists", None)
        if not current and exists:
            # A UID SEARCH that claims a non-empty folder has no messages would
            # otherwise look like "everything was deleted" and write removals
            # for the whole folder.  Treat it as a server error instead.
            raise ImapError(
                f"UID SEARCH returned no UIDs for {folder.name!r}, "
                f"but the folder reports {exists} message(s)"
            )
        if current and exists is not None and len(current) != exists:
            self.log.warning(
                "[%s] UID SEARCH returned %d of %d message(s); "
                "continuing with the returned set",
                folder.name,
                len(current),
                exists,
            )

        logfile = ActiveLog(self.store, folder.name, self.config.max_ops_per_file)
        entry = state["folders"].setdefault(folder.name, {})

        if dry_run:
            if logfile.reset is None:
                summary.baseline = True
                summary.added = len(current)
            elif (
                logfile.uidvalidity
                and uidvalidity
                and logfile.uidvalidity != uidvalidity
            ):
                summary.reset = True
                summary.added = len(current)
            else:
                known = logfile.state
                summary.removed = sum(1 for uid in known if uid not in current_set)
                summary.added = sum(1 for uid in current if uid not in known)
            return

        if logfile.reset is None:
            # First file for this folder: the reset baseline lists everything
            # present now, so no individual add records are needed.
            occurrences = self._fetch_all(folder, current, summary)
            if occurrences:
                logfile.start(cycle, uidvalidity, occurrences)
                logfile.save()
                summary.baseline = True
            summary.added = len(occurrences)
        elif (
            logfile.uidvalidity
            and uidvalidity
            and logfile.uidvalidity != uidvalidity
        ):
            # UIDVALIDITY changed: begin a new file with a new baseline.  The
            # old file remains intact for restores before this point.
            self.log.warning(
                "[%s] UIDVALIDITY changed %s -> %s; starting a new baseline file",
                folder.name,
                logfile.uidvalidity,
                uidvalidity,
            )
            occurrences = self._fetch_all(folder, current, summary)
            logfile.reset_to(cycle, uidvalidity, occurrences)
            logfile.save()
            summary.reset = True
            summary.added = len(occurrences)
        else:
            known = logfile.state
            removed = [uid for uid in known if uid not in current_set]
            new = [uid for uid in current if uid not in known]
            summary.removed = len(removed)
            for uid in removed:
                occurrence = known[uid]
                logfile.append(
                    Record.remove(cycle, folder.name, uid, occurrence.key),
                    cycle,
                    uidvalidity,
                )
            summary.added = self._fetch_new(
                folder, new, cycle, uidvalidity, logfile, summary
            )
            summary.snapshot = logfile.maybe_roll_time(
                cycle,
                now,
                self.config.snapshot_every_seconds,
                uidvalidity,
            )
            logfile.save()

        entry["uidvalidity"] = uidvalidity
        entry["last_uid"] = (
            max(current) if current else int(entry.get("last_uid") or 0)
        )
        entry["last_scan"] = iso(now)
        entry["seq"] = logfile.seq
        entry["ops"] = logfile.ops_count
        entry["file_start"] = logfile.file_start
        self.store.save_state(state)

    # -- fetching ---------------------------------------------------------

    def _fetch_all(
        self, folder: Folder, uids: list[int], summary: FolderSummary
    ) -> list[Occurrence]:
        occurrences: list[Occurrence] = []
        for batch in _batches(uids, self.config.imap_batch_size):
            messages = self._fetch_with_retry(folder, batch)
            got = {message.uid: message for message in messages}
            for uid in batch:
                message = got.get(uid)
                if message is None:
                    summary.vanished += 1
                    continue
                occurrences.append(self._store_message(uid, message, summary))
        return occurrences

    def _fetch_new(
        self,
        folder: Folder,
        uids: list[int],
        cycle: str,
        uidvalidity: int,
        logfile: ActiveLog,
        summary: FolderSummary,
    ) -> int:
        added = 0
        for batch in _batches(uids, self.config.imap_batch_size):
            messages = self._fetch_with_retry(folder, batch)
            got = {message.uid: message for message in messages}
            for uid in batch:
                message = got.get(uid)
                if message is None:
                    summary.vanished += 1
                    continue
                occurrence = self._store_message(uid, message, summary)
                logfile.append(
                    Record.add(cycle, folder.name, occurrence), cycle, uidvalidity
                )
                added += 1
        return added

    def _store_message(
        self, uid: int, message: FetchedMessage, summary: FolderSummary
    ) -> Occurrence:
        key, created = self.store.put_blob(message.raw)
        if created:
            summary.new_blobs += 1
            summary.new_bytes += len(message.raw)
        return Occurrence(
            uid=uid,
            key=key,
            flags=tuple(sorted(message.flags)),
            message_id=message_id_of(message.raw),
            size=len(message.raw),
        )

    # -- connection -------------------------------------------------------

    def _ensure_connection(self) -> None:
        if self.client.conn is None:
            self.client.connect()
            return
        try:
            typ, _ = self.client.conn.noop()
            if typ != "OK":
                raise imaplib.IMAP4.abort(f"NOOP returned {typ}")
        except (imaplib.IMAP4.abort, ssl.SSLError, OSError):
            self.log.info("IMAP connection dropped; reconnecting")
            self.client.reconnect()

    def _select(self, folder: Folder) -> None:
        try:
            self.client.select(folder)
        except (imaplib.IMAP4.abort, ssl.SSLError, OSError):
            self.log.info(
                "[%s] connection lost while selecting; reconnecting", folder.name
            )
            self.client.reconnect()
            self.client.select(folder)

    def _fetch_with_retry(
        self, folder: Folder, batch: list[int]
    ) -> list[FetchedMessage]:
        attempts = self.config.imap_retries
        for attempt in range(attempts + 1):
            try:
                return self.client.fetch(batch)
            except (imaplib.IMAP4.abort, ssl.SSLError, OSError) as exc:
                if attempt >= attempts:
                    raise ImapError(
                        f"giving up on {folder.name!r} after {attempts} retries: {exc}"
                    ) from exc
                delay = min(2**attempt, 30)
                self.log.warning(
                    "[%s] connection problem (%s); reconnecting in %ds",
                    folder.name,
                    exc,
                    delay,
                )
                time.sleep(delay)
                self.client.reconnect()
                self.client.select(folder)
        raise AssertionError("unreachable")

    # -- upload -----------------------------------------------------------

    def _flush(self, summary: CycleSummary) -> None:
        if isinstance(self.backend, NullBackend):
            return
        for key in list(self.store.dirty_blobs):
            if self.store.remote_blob_key(key) in self.remote_keys:
                self.store.mark_blob_clean(key)
        # Log files are different: the active file is rewritten as operations
        # are appended, so a key that exists remotely may hold an older
        # version.  Always upload every dirty log file.
        pending_blobs = [
            key
            for key in sorted(self.store.dirty_blobs)
            if self.store.remote_blob_key(key) not in self.remote_keys
        ]
        if pending_blobs:
            self.log.info("uploading %d message(s)", len(pending_blobs))
            errors: list[str] = []
            completed = 0
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(self._upload_blob, key): key
                    for key in pending_blobs
                }
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        size = future.result()
                    except Exception as exc:  # noqa: BLE001  # collect all failures
                        errors.append(f"{key}: {exc}")
                        continue
                    summary.uploaded_blobs += 1
                    summary.uploaded_bytes += size
                    self.store.mark_blob_clean(key)
                    self.remote_keys.add(self.store.remote_blob_key(key))
                    completed += 1
                    if completed % 250 == 0:
                        self.log.info(
                            "  %d/%d message(s) uploaded",
                            completed,
                            len(pending_blobs),
                        )
            if errors:
                raise BackupError(
                    f"{len(errors)} message upload(s) failed; first: {errors[0]}"
                )

        pending_logs = sorted(self.store.dirty_logs)
        if pending_logs:
            self.log.info("uploading %d log file(s)", len(pending_logs))
            for path in pending_logs:
                key = self.store.remote_key(path)
                self.backend.put_file(key, path)
                summary.uploaded_logs += 1
                summary.uploaded_bytes += path.stat().st_size
                self.store.mark_log_clean(path)
                self.remote_keys.add(key)

    def _upload_blob(self, key: str) -> int:
        path = self.store.blob_path(key)
        self.backend.put_file(self.store.remote_blob_key(key), path)
        return path.stat().st_size

    def _upload_state(self, state: dict) -> None:
        data = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.backend.put_bytes("state.json", data)
        self.remote_keys.add("state.json")

    def _download_many(self, keys: list[str]) -> None:
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._download_one, key): key for key in keys}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001  # collect all failures
                    errors.append(f"{key}: {exc}")
        if errors:
            raise BackupError(
                f"{len(errors)} download(s) failed; first: {errors[0]}"
            )

    def _download_one(self, key: str) -> None:
        root = self.store.root.resolve()
        path = (root / key).resolve()
        if not path.is_relative_to(root):
            raise BackupError(f"refusing to write outside the data directory: {key}")
        self.backend.get_file(key, path)

    def close(self) -> None:
        self.client.close()


def _batches(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
