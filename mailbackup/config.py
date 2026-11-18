"""Configuration from the environment, with optional CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from .util import ConfigError, as_bool


@dataclass(frozen=True)
class Config:
    # IMAP
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = field(default="", repr=False)
    imap_ssl: bool = True
    imap_folders: tuple[str, ...] | None = None
    imap_all_folders: bool = False
    imap_batch_size: int = 50
    imap_timeout: int = 60
    imap_retries: int = 3
    # Local + remote
    data_dir: Path = Path("./data")
    backend: str = "s3"
    backup_dir: Path | None = None
    s3_bucket: str | None = None
    s3_prefix: str = ""
    s3_endpoint_url: str | None = None
    aws_region: str | None = None
    s3_storage_class: str | None = None
    s3_sse: str | None = None
    s3_kms_key_id: str | None = None
    # Schedule / behaviour
    interval_seconds: int = 300
    max_ops_per_file: int = 5000
    snapshot_every_seconds: int = 604800
    log_level: str = "INFO"
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.imap_port <= 0:
            raise ConfigError("IMAP_PORT must be positive")
        if self.imap_batch_size < 1:
            raise ConfigError("IMAP_BATCH_SIZE must be at least 1")
        if self.imap_timeout < 1:
            raise ConfigError("IMAP_TIMEOUT must be at least 1")
        if self.imap_retries < 0:
            raise ConfigError("IMAP_RETRIES cannot be negative")
        if self.interval_seconds < 1:
            raise ConfigError("INTERVAL_SECONDS must be at least 1")
        if self.max_ops_per_file < 2:
            raise ConfigError(
                "MAX_OPS_PER_FILE must be at least 2 (a reset plus one operation)"
            )
        if self.snapshot_every_seconds < 1:
            raise ConfigError("SNAPSHOT_EVERY_SECONDS must be at least 1")
        if self.backend not in {"s3", "dir", "null"}:
            raise ConfigError(
                f"BACKEND must be s3, dir, or null (got {self.backend!r})"
            )
        if self.backend == "s3" and not self.dry_run and not self.s3_bucket:
            raise ConfigError("S3_BUCKET is required")
        if self.backend == "dir" and self.backup_dir is None:
            raise ConfigError("BACKUP_DIR is required for BACKEND=dir")

    @classmethod
    def from_env(
        cls, overrides: dict | None = None, require_imap: bool = True
    ) -> Config:
        env = os.environ
        values: dict = {}
        overrides = overrides or {}

        def set_value(name: str, value):
            if name in overrides and overrides[name] is not None:
                values[name] = overrides[name]
            elif value is not None:
                values[name] = value

        set_value("imap_host", env.get("IMAP_HOST"))
        set_value("imap_port", _int(env, "IMAP_PORT"))
        set_value("imap_user", env.get("IMAP_USER"))
        set_value("imap_ssl", _bool(env, "IMAP_SSL", True))
        set_value("imap_all_folders", _bool(env, "IMAP_ALL_FOLDERS", False))
        set_value("imap_folders", _folders(env.get("IMAP_FOLDERS")))
        set_value("imap_batch_size", _int(env, "IMAP_BATCH_SIZE"))
        set_value("imap_timeout", _int(env, "IMAP_TIMEOUT"))
        set_value("imap_retries", _int(env, "IMAP_RETRIES"))
        set_value("data_dir", _path(env.get("DATA_DIR")))
        set_value("backend", env.get("BACKEND") or None)
        set_value("backup_dir", _path(env.get("BACKUP_DIR")))
        set_value("s3_bucket", env.get("S3_BUCKET"))
        set_value("s3_prefix", (env.get("S3_PREFIX") or "").strip("/"))
        set_value("s3_endpoint_url", env.get("S3_ENDPOINT_URL") or None)
        set_value(
            "aws_region",
            env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or None,
        )
        set_value("s3_storage_class", env.get("S3_STORAGE_CLASS") or None)
        set_value("s3_sse", env.get("S3_SSE") or None)
        set_value("s3_kms_key_id", env.get("S3_KMS_KEY_ID") or None)
        set_value("interval_seconds", _int(env, "INTERVAL_SECONDS"))
        set_value("max_ops_per_file", _int(env, "MAX_OPS_PER_FILE"))
        set_value(
            "snapshot_every_seconds", _int(env, "SNAPSHOT_EVERY_SECONDS")
        )
        set_value("log_level", (env.get("LOG_LEVEL") or "INFO").upper())
        set_value("dry_run", _bool(env, "DRY_RUN", False))

        password = env.get("IMAP_PASSWORD")
        if "imap_password" in overrides and overrides["imap_password"] is not None:
            password = overrides["imap_password"]
        else:
            password_file = env.get("IMAP_PASSWORD_FILE")
            if password_file:
                password = _read_secret(Path(password_file))
        set_value("imap_password", password)

        config = cls(**values)
        if require_imap:
            missing = [
                label
                for label, value in (
                    ("IMAP_HOST", config.imap_host),
                    ("IMAP_USER", config.imap_user),
                    ("IMAP_PASSWORD_FILE or IMAP_PASSWORD", config.imap_password),
                )
                if not value
            ]
            if missing:
                raise ConfigError("missing required setting(s): " + ", ".join(missing))
        return config

    def replace(self, **changes) -> Config:
        data = {field.name: getattr(self, field.name) for field in fields(self)}
        data.update(changes)
        return Config(**data)


def _int(env, name: str) -> int | None:
    raw = env.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(env, name: str, default: bool) -> bool:
    return as_bool(env.get(name), default)


def _path(raw: str | None) -> Path | None:
    if not raw:
        return None
    return Path(raw).expanduser()


def _folders(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or None


def _read_secret(path: Path) -> str:
    try:
        data = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read IMAP password file {path}: {exc}") from exc
    # Compose secrets end with a newline; keep everything else (including
    # leading/trailing spaces) because passwords may legitimately contain it.
    secret = data.rstrip("\r\n")
    if not secret:
        raise ConfigError(f"IMAP password file {path} is empty")
    return secret
