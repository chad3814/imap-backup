"""Point-in-time mbox reconstruction from numbered JSONL logs and blobs.

Each numbered file is self-contained: it starts with a reset baseline listing
the folder, then add/rm operations.  A mirror restore therefore reads exactly
one file — the newest one whose baseline is at or before the requested time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .backend import Backend
from .events import Occurrence, Record, parse_jsonl_bytes, replay
from .mbox import render_mbox
from .store import LocalStore
from .util import (
    atomic_write_text,
    cycle_name,
    human_bytes,
    parse_log_file_name,
    parse_when,
    safe_stem,
    utcnow,
)

log = logging.getLogger("mailbackup.restore")


class RestoreError(Exception):
    pass


@dataclass
class FolderSelection:
    folder: str
    occurrences: list[Occurrence]
    baseline: str | None
    source_file: str | None
    files_read: int


@dataclass
class RestoreResult:
    cutoff: str
    folders: dict[str, FolderSelection] = field(default_factory=dict)
    missing_blobs: list[str] = field(default_factory=list)

    @property
    def messages(self) -> int:
        return sum(len(selection.occurrences) for selection in self.folders.values())


class RestoreEngine:
    def __init__(self, backend: Backend, cache_dir: Path, jobs: int = 8):
        self.backend = backend
        self.cache = LocalStore(cache_dir)
        self.jobs = max(1, jobs)

    # -- log collection ---------------------------------------------------

    def collect(
        self,
        at: str | None = None,
        folders: set[str] | None = None,
        keep_removed: bool = False,
        include_uncommitted: bool = False,
    ) -> RestoreResult:
        # Validate the timestamp before touching the backend, so a typo gets a
        # clear argument error even when the destination is empty.
        requested = cycle_name(parse_when(at)) if at else None
        keys = self.backend.list_keys("mboxlogs/")
        if not keys and not self.backend.exists("state.json"):
            raise RestoreError(
                "no backup found (no mboxlogs/ objects and no state.json)"
            )

        state = self._load_state()
        last_cycle = str(state.get("last_cycle") or "")
        if not last_cycle:
            last_cycle = cycle_name(utcnow())
            log.warning(
                "state.json has no last_cycle; using the current time "
                "(segments beyond the last commit are included)"
            )

        if requested:
            if include_uncommitted:
                cutoff = requested
            else:
                cutoff = min(requested, last_cycle)
                if requested > last_cycle:
                    log.warning(
                        "requested %s is newer than the last commit (%s); "
                        "using the last commit",
                        requested,
                        last_cycle,
                    )
        else:
            cutoff = cycle_name(utcnow()) if include_uncommitted else last_cycle

        grouped: dict[str, list[tuple[int, str]]] = {}
        for key in keys:
            parsed = self._parse_key(key)
            if parsed is None:
                log.warning("ignoring unexpected log key %s", key)
                continue
            folder, seq, _key = parsed
            grouped.setdefault(folder, []).append((seq, key))

        if folders:
            missing = folders - set(grouped)
            if missing:
                available = ", ".join(sorted(grouped)) or "none"
                raise RestoreError(
                    f"folder(s) not found: {', '.join(sorted(missing))} "
                    f"(available: {available})"
                )

        result = RestoreResult(cutoff=cutoff)
        for folder, entries in sorted(grouped.items()):
            if folders and folder not in folders:
                continue
            entries.sort()
            if keep_removed:
                selection = self._collect_archive(folder, entries, cutoff)
            else:
                selection = self._collect_mirror(folder, entries, cutoff)
            if selection is not None:
                result.folders[folder] = selection
        return result

    def _collect_mirror(
        self, folder: str, entries: list[tuple[int, str]], cutoff: str
    ) -> FolderSelection | None:
        """Read the newest file whose reset baseline is at or before cutoff."""
        for seq, key in reversed(entries):
            records = self._load_file(folder, key)
            if records[0].cycle > cutoff:
                continue
            return FolderSelection(
                folder=folder,
                occurrences=replay(records, cutoff=cutoff),
                baseline=records[0].cycle,
                source_file=key,
                files_read=1,
            )
        return None

    def _collect_archive(
        self, folder: str, entries: list[tuple[int, str]], cutoff: str
    ) -> FolderSelection | None:
        """Union every file up to cutoff (used by --keep-removed)."""
        if not entries:
            return None
        # The reset baselines are non-decreasing in seq, so stop at the first
        # file that starts after the cutoff.
        selected: list[tuple[int, str, list[Record]]] = []
        for seq, key in entries:
            records = self._load_file(folder, key)
            if records[0].cycle > cutoff:
                break
            selected.append((seq, key, records))
        if not selected:
            return None
        combined = [
            record for _seq, _key, records in selected for record in records
        ]
        return FolderSelection(
            folder=folder,
            occurrences=replay(combined, cutoff=cutoff, keep_removed=True),
            baseline=combined[0].cycle,
            source_file=None,
            files_read=len(selected),
        )

    def _load_file(self, folder: str, key: str) -> list[Record]:
        try:
            data = self.backend.get_bytes(key)
        except Exception as exc:  # any backend/transport failure
            raise RestoreError(f"cannot read {key}: {exc}") from exc
        try:
            records = parse_jsonl_bytes(data, origin=key)
        except ValueError as exc:
            raise RestoreError(str(exc)) from exc
        self._validate(folder, key, records)
        return records

    @staticmethod
    def _validate(folder: str, key: str, records: list[Record]) -> None:
        if not records:
            raise RestoreError(f"{key} is empty; a reset baseline is required")
        if records[0].type != "reset":
            raise RestoreError(f"{key} does not start with a reset baseline")
        if any(record.type == "reset" for record in records[1:]):
            raise RestoreError(f"{key} has a reset after its first line")
        for record in records:
            if record.folder != folder:
                raise RestoreError(
                    f"{key} belongs to {record.folder!r}, not {folder!r}"
                )

    def _parse_key(self, key: str) -> tuple[str, int, str] | None:
        parts = key.split("/")
        if len(parts) != 2 or parts[0] != "mboxlogs":
            return None
        parsed = parse_log_file_name(parts[1])
        if parsed is None:
            return None
        folder, seq = parsed
        return folder, seq, key

    def _load_state(self) -> dict:
        try:
            data = self.backend.get_bytes("state.json")
        except Exception as exc:  # noqa: BLE001  # state may legitimately be absent
            log.warning("state.json is unavailable (%s); deriving from logs", exc)
            return {}
        try:
            state = json.loads(data)
        except ValueError as exc:
            raise RestoreError(f"remote state.json is invalid: {exc}") from exc
        if not isinstance(state, dict):
            raise RestoreError("remote state.json is not an object")
        return state

    # -- restore ----------------------------------------------------------

    def run(
        self,
        out_dir: Path,
        at: str | None = None,
        folders: set[str] | None = None,
        keep_removed: bool = False,
        include_uncommitted: bool = False,
        limit: int | None = None,
        allow_missing: bool = False,
    ) -> dict:
        result = self.collect(
            at=at,
            folders=folders,
            keep_removed=keep_removed,
            include_uncommitted=include_uncommitted,
        )
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        cutoff_dt = parse_when(result.cutoff)
        used_names: set[str] = set()
        manifest: dict = {
            "generated_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cutoff": result.cutoff,
            "mode": "keep-removed" if keep_removed else "mirror",
            "folders": {},
            "missing_blobs": [],
        }
        selected: dict[str, FolderSelection] = {}
        for folder, selection in sorted(result.folders.items()):
            occurrences = selection.occurrences
            if limit is not None:
                occurrences = occurrences[:limit]
            selected[folder] = FolderSelection(
                folder=folder,
                occurrences=occurrences,
                baseline=selection.baseline,
                source_file=selection.source_file,
                files_read=selection.files_read,
            )

        all_keys = sorted(
            {occ.key for selection in selected.values() for occ in selection.occurrences}
        )
        missing = self._ensure_blobs(all_keys)
        if missing and not allow_missing:
            raise RestoreError(
                f"{len(missing)} message blob(s) are missing; first: {missing[0]}"
            )
        result.missing_blobs = missing
        missing_set = set(missing)
        # Only now that every required blob is present is it safe to remove
        # the previous run's mboxes.
        _remove_previous_output(out_dir)

        for folder, selection in sorted(selected.items()):
            occurrences = selection.occurrences
            available = [
                occurrence
                for occurrence in occurrences
                if occurrence.key not in missing_set
            ]
            stem = safe_stem(folder, used_names)
            path = out_dir / f"{stem}.mbox"
            if not available:
                manifest["folders"][folder] = {
                    "file": None,
                    "messages": 0,
                    "bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "baseline": selection.baseline,
                    "source_file": selection.source_file,
                    "files_read": selection.files_read,
                }
                continue
            count, total, digest = render_mbox(
                available,
                self.cache.read_blob,
                path,
                cutoff_dt,
            )
            manifest["folders"][folder] = {
                "file": path.name,
                "messages": count,
                "bytes": total,
                "sha256": digest,
                "baseline": selection.baseline,
                "source_file": selection.source_file,
                "files_read": selection.files_read,
            }
            log.info(
                "[%s] wrote %d message(s), %s (%s%s)",
                folder,
                count,
                human_bytes(total),
                path.name,
                f", {len(occurrences) - len(available)} missing"
                if missing_set
                else "",
            )

        manifest["missing_blobs"] = missing[:100]
        atomic_write_text(
            out_dir / "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
        log.info(
            "restored %d message(s) in %d folder(s) at %s into %s",
            sum(len(sel.occurrences) for sel in selected.values()) - len(missing),
            len(selected),
            result.cutoff,
            out_dir,
        )
        return manifest

    # -- verification -----------------------------------------------------

    def verify(
        self,
        at: str | None = None,
        folders: set[str] | None = None,
        keep_removed: bool = False,
        include_uncommitted: bool = False,
    ) -> tuple[RestoreResult, list[str]]:
        result = self.collect(
            at=at,
            folders=folders,
            keep_removed=keep_removed,
            include_uncommitted=include_uncommitted,
        )
        keys = sorted(
            {occ.key for sel in result.folders.values() for occ in sel.occurrences}
        )
        missing: list[str] = []
        present = 0
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = {
                pool.submit(self._blob_present, key): key for key in keys
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    if future.result():
                        present += 1
                    else:
                        missing.append(key)
                except Exception:  # noqa: BLE001  # treat any error as missing
                    missing.append(key)
        return result, sorted(missing)

    def _blob_present(self, key: str) -> bool:
        if self.cache.blob_path(key).is_file():
            return True
        return self.backend.exists(self.cache.remote_blob_key(key))

    # -- blobs ------------------------------------------------------------

    def _ensure_blobs(self, keys: list[str]) -> list[str]:
        missing: list[str] = []
        todo = [key for key in keys if not self.cache.blob_path(key).is_file()]
        if todo:
            log.info("downloading %d message blob(s)", len(todo))
        completed = 0
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = {
                pool.submit(self._download_blob, key): key for key in todo
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001  # collect all failures
                    errors.append(f"{key}: {exc}")
                completed += 1
                if completed % 500 == 0:
                    log.info("  %d/%d blob(s) downloaded", completed, len(todo))
        for key in keys:
            if not self.cache.blob_path(key).is_file():
                missing.append(key)
        if errors:
            log.warning(
                "%d blob download error(s); first: %s", len(errors), errors[0]
            )
        return missing

    def _download_blob(self, key: str) -> None:
        target = self.cache.blob_path(key)
        if target.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.{id(target)}.tmp")
        try:
            self.backend.get_file(self.cache.remote_blob_key(key), tmp)
            digest = hashlib.sha256()
            with tmp.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != key:
                raise RestoreError(
                    f"blob {key} failed its SHA-256 check (corrupt object)"
                )
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()


def _remove_previous_output(out_dir: Path) -> None:
    """Delete mbox files listed in a previous restore's manifest.

    Restoring twice into the same directory would otherwise leave stale mboxes
    for folders that are empty at the new cutoff.  Only files recorded in our
    own manifest are touched, and only direct children of ``out_dir``.
    """
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(previous, dict):
        return
    if previous.get("mode") not in {"mirror", "keep-removed"}:
        return
    for entry in previous.get("folders", {}).values():
        if not isinstance(entry, dict):
            continue
        name = entry.get("file")
        if not isinstance(name, str) or not name.endswith(".mbox"):
            continue
        if Path(name).name != name:
            continue
        try:
            (out_dir / name).unlink()
        except FileNotFoundError:
            pass
