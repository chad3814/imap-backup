"""Command line entry point."""

from __future__ import annotations

import argparse
import imaplib
import json
import logging
import os
import ssl
import sys
import time
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from . import __version__
from .backend import make_backend
from .backup import BackupEngine, BackupError
from .config import Config
from .daemon import run_forever
from .imap import ImapClient, ImapError
from .restore import RestoreEngine, RestoreError
from .store import LocalStore, StoreError
from .util import ConfigError, human_bytes, parse_when, utcnow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailbackup",
        description=(
            "Content-addressed IMAP backup to S3 with point-in-time mbox restore."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="run one backup cycle")
    _add_imap_args(backup)
    _add_backend_args(backup)
    backup.add_argument("--dry-run", action="store_true", help="count changes only")

    daemon = sub.add_parser("daemon", help="run a cycle every INTERVAL_SECONDS")
    _add_imap_args(daemon)
    _add_backend_args(daemon)
    daemon.add_argument(
        "--interval", type=int, help="seconds between cycles (default 300)"
    )

    restore = sub.add_parser("restore", help="generate mbox files at a point in time")
    _add_backend_args(restore)
    restore.add_argument("--out", required=True, help="output directory")
    restore.add_argument("--at", help="ISO timestamp or YYYY-MM-DD")
    restore.add_argument(
        "--folder", action="append", help="only this folder (repeatable)"
    )
    restore.add_argument(
        "--keep-removed",
        action="store_true",
        help="include messages later removed or moved away",
    )
    restore.add_argument(
        "--include-uncommitted",
        action="store_true",
        help="include segments newer than last_cycle",
    )
    restore.add_argument("--limit", type=int, help="max messages per folder")
    restore.add_argument("--jobs", type=int, default=8, help="parallel downloads")
    restore.add_argument(
        "--allow-missing",
        action="store_true",
        help="skip (and report) missing message blobs instead of failing",
    )

    verify = sub.add_parser("verify", help="check that every referenced blob exists")
    _add_backend_args(verify)
    verify.add_argument("--at", help="ISO timestamp or YYYY-MM-DD")
    verify.add_argument("--folder", action="append")
    verify.add_argument("--keep-removed", action="store_true")
    verify.add_argument("--include-uncommitted", action="store_true")
    verify.add_argument("--jobs", type=int, default=8)

    listing = sub.add_parser("list", help="list folders and message counts")
    _add_backend_args(listing)
    listing.add_argument("--at", help="ISO timestamp or YYYY-MM-DD")
    listing.add_argument("--jobs", type=int, default=8)

    health = sub.add_parser("health", help="check the age of the last completed cycle")
    health.add_argument("--data-dir", help="directory containing state.json")
    health.add_argument(
        "--max-age", type=int, default=900, help="maximum age in seconds (default 900)"
    )

    sub.add_parser("version", help="print the version")
    return parser


def _add_imap_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="IMAP host (or IMAP_HOST)")
    parser.add_argument("--port", type=int, help="IMAP port (or IMAP_PORT)")
    parser.add_argument("--user", help="IMAP user (or IMAP_USER)")
    parser.add_argument(
        "--folders", help="comma-separated folders (default: all but junk/trash)"
    )
    parser.add_argument(
        "--all-folders", action="store_true", help="include Junk, Trash and All Mail"
    )
    parser.add_argument("--no-ssl", action="store_true", help="disable TLS")


def _add_backend_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend", choices=("s3", "dir", "null"), help="storage backend"
    )
    parser.add_argument("--bucket", help="S3 bucket (or S3_BUCKET)")
    parser.add_argument("--prefix", help="S3 key prefix (or S3_PREFIX)")
    parser.add_argument("--region", help="AWS region (or AWS_REGION)")
    parser.add_argument("--endpoint-url", help="S3 endpoint (or S3_ENDPOINT_URL)")
    parser.add_argument("--backup-dir", help="directory for the dir backend")
    parser.add_argument("--data-dir", help="local working/cache directory")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")


def _overrides(args: argparse.Namespace) -> dict:
    mapping = {
        "host": "imap_host",
        "port": "imap_port",
        "user": "imap_user",
        "bucket": "s3_bucket",
        "prefix": "s3_prefix",
        "region": "aws_region",
        "endpoint_url": "s3_endpoint_url",
        "backup_dir": "backup_dir",
        "data_dir": "data_dir",
        "backend": "backend",
        "interval": "interval_seconds",
        "log_level": "log_level",
    }
    values: dict = {}
    for source, target in mapping.items():
        value = getattr(args, source, None)
        if value is not None:
            values[target] = value
    folders = getattr(args, "folders", None)
    if folders:
        values["imap_folders"] = tuple(
            part.strip() for part in folders.split(",") if part.strip()
        )
    if getattr(args, "all_folders", False):
        values["imap_all_folders"] = True
    if getattr(args, "no_ssl", False):
        values["imap_ssl"] = False
    if getattr(args, "dry_run", False):
        values["dry_run"] = True
    return values


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, (level or "INFO").upper(), logging.INFO)
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
        force=True,
    )
    for noisy in ("boto3", "botocore", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _build_engine(config: Config) -> BackupEngine:
    store = LocalStore(config.data_dir)
    backend = make_backend(config)
    client = ImapClient(
        host=config.imap_host,
        port=config.imap_port,
        user=config.imap_user,
        password=config.imap_password,
        use_ssl=config.imap_ssl,
        timeout=config.imap_timeout,
    )
    return BackupEngine(config, store, client, backend)


def _restore_engine(config: Config, jobs: int) -> RestoreEngine:
    backend = make_backend(config)
    return RestoreEngine(backend, config.data_dir, jobs=jobs)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "log_level", None) or "INFO")

    if args.command == "version":
        print(f"mailbackup {__version__}")
        return 0

    if args.command == "health":
        return _health(args)

    try:
        if args.command in {"backup", "daemon"}:
            config = Config.from_env(_overrides(args), require_imap=True)
            engine = _build_engine(config)
            try:
                if args.command == "backup":
                    summary = engine.run_cycle()
                    if summary.errors:
                        return 1
                    return 0
                return run_forever(engine, config)
            finally:
                engine.close()

        config = Config.from_env(_overrides(args), require_imap=False)
        folders = set(args.folder) if getattr(args, "folder", None) else None
        engine = _restore_engine(config, args.jobs)

        if args.command == "list":
            result = engine.collect(at=args.at)
            if not result.folders:
                print("no folders")
                return 0
            for folder, selection in sorted(result.folders.items()):
                print(f"{folder}: {len(selection.occurrences)} message(s)")
            return 0

        if args.command == "verify":
            result, missing = engine.verify(
                at=args.at,
                folders=folders,
                keep_removed=args.keep_removed,
                include_uncommitted=args.include_uncommitted,
            )
            for folder, selection in sorted(result.folders.items()):
                print(f"{folder}: {len(selection.occurrences)} message(s)")
            print(
                f"checked {result.messages} message reference(s) at {result.cutoff}: "
                f"{result.messages - len(missing)} present, {len(missing)} missing"
            )
            for key in missing[:20]:
                print(f"  missing: {key}")
            return 1 if missing else 0

        if args.command == "restore":
            if args.limit is not None and args.limit < 1:
                parser.error("--limit must be at least 1")
            manifest = engine.run(
                out_dir=Path(args.out),
                at=args.at,
                folders=folders,
                keep_removed=args.keep_removed,
                include_uncommitted=args.include_uncommitted,
                limit=args.limit,
                allow_missing=args.allow_missing,
            )
            total = sum(
                entry["messages"] for entry in manifest["folders"].values()
            )
            total_bytes = sum(
                entry["bytes"] for entry in manifest["folders"].values()
            )
            print(
                f"restored {total} message(s), {human_bytes(total_bytes)} "
                f"in {len(manifest['folders'])} folder(s) at {manifest['cutoff']}"
            )
            return 0

        parser.error(f"unknown command {args.command}")
        return 2
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except (BackupError, RestoreError, ImapError, StoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (BotoCoreError, ClientError) as exc:
        print(f"S3 error: {exc}", file=sys.stderr)
        return 1
    except imaplib.IMAP4.error as exc:
        print(f"IMAP error: {exc}", file=sys.stderr)
        return 1
    except ssl.SSLError as exc:
        print(f"TLS error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"system error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; progress is saved", file=sys.stderr)
        return 130


def _health(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir or os.environ.get("DATA_DIR") or "./data")
    state_path = data_dir / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"unhealthy: cannot read {state_path}: {exc}", file=sys.stderr)
        return 1
    last = state.get("last_cycle") or ""
    if not last:
        print("unhealthy: no completed cycle yet", file=sys.stderr)
        return 1
    try:
        age = (utcnow() - parse_when(last)).total_seconds()
    except ValueError as exc:
        print(f"unhealthy: bad last_cycle {last!r}: {exc}", file=sys.stderr)
        return 1
    if age > args.max_age:
        print(
            f"unhealthy: last completed cycle was {int(age)}s ago "
            f"(limit {args.max_age}s)",
            file=sys.stderr,
        )
        return 1
    print(f"ok: last completed cycle {int(age)}s ago")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
