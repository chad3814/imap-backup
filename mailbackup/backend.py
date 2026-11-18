"""Remote storage backends: S3 (the real one) and a directory (tests/offline)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .util import atomic_write_bytes


def _content_type(name: str) -> str:
    if name.endswith(".eml"):
        return "message/rfc822"
    if name.endswith(".jsonl"):
        return "application/x-ndjson"
    if name.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


class Backend:
    def list_keys(self, prefix: str = "") -> set[str]:
        raise NotImplementedError

    def put_file(self, key: str, path: Path) -> None:
        raise NotImplementedError

    def put_bytes(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def get_file(self, key: str, path: Path) -> None:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def size(self, key: str) -> int | None:
        return None

    def close(self) -> None:
        pass


class S3Backend(Backend):
    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
        storage_class: str | None = None,
        sse: str | None = None,
        kms_key_id: str | None = None,
    ):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.storage_class = storage_class or None
        self.sse = sse or None
        self.kms_key_id = kms_key_id or None

        session = boto3.session.Session(region_name=region)
        client_config = Config(
            retries={"max_attempts": 5, "mode": "standard"},
            user_agent_extra="mailbackup/1.0",
        )
        if endpoint_url:
            # MinIO and most S3-compatible endpoints want path-style
            # addressing rather than bucket.<endpoint> DNS names.
            client_config = Config(
                retries={"max_attempts": 5, "mode": "standard"},
                user_agent_extra="mailbackup/1.0",
                s3={"addressing_style": "path"},
            )
        client_kwargs = {"config": client_config}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
        self.client = session.client("s3", **client_kwargs)

    # -- key handling -----------------------------------------------------

    def full_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def relative_key(self, key: str) -> str:
        if self.prefix:
            marker = self.prefix + "/"
            if key.startswith(marker):
                return key[len(marker) :]
        return key

    def _put_extra(self, key: str) -> dict:
        extra = {"ContentType": _content_type(key)}
        if self.sse:
            extra["ServerSideEncryption"] = self.sse
        if self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id
        if self.storage_class:
            extra["StorageClass"] = self.storage_class
        return extra

    # -- Backend ----------------------------------------------------------

    def list_keys(self, prefix: str = "") -> set[str]:
        keys: set[str] = set()
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=self.full_key(prefix)
        ):
            for item in page.get("Contents", []):
                name = item["Key"]
                if name.endswith("/"):
                    continue
                keys.add(self.relative_key(name))
        return keys

    def put_file(self, key: str, path: Path) -> None:
        self.client.upload_file(
            str(path),
            self.bucket,
            self.full_key(key),
            ExtraArgs=self._put_extra(key),
        )

    def put_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.full_key(key),
            Body=data,
            **self._put_extra(key),
        )

    def get_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(
            self.bucket, self.full_key(key), str(path)
        )

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(
            Bucket=self.bucket, Key=self.full_key(key)
        )
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        return self.size(key) is not None

    def size(self, key: str) -> int | None:
        from botocore.exceptions import ClientError

        try:
            response = self.client.head_object(
                Bucket=self.bucket, Key=self.full_key(key)
            )
            return int(response["ContentLength"])
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"S3Backend(bucket={self.bucket!r}, prefix={self.prefix!r})"


class DirBackend(Backend):
    """A directory standing in for S3.  Useful for tests and offline mirrors."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if key.startswith("/") or ".." in Path(key).parts:
            raise ValueError(f"unsafe key {key!r}")
        return self.root / key

    def list_keys(self, prefix: str = "") -> set[str]:
        keys: set[str] = set()
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                keys.add(key)
        return keys

    def put_file(self, key: str, path: Path) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        shutil.copyfile(path, tmp)
        os.replace(tmp, target)

    def put_bytes(self, key: str, data: bytes) -> None:
        atomic_write_bytes(self._path(key), data)

    def get_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path(key), path)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def size(self, key: str) -> int | None:
        try:
            return self._path(key).stat().st_size
        except FileNotFoundError:
            return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"DirBackend(root={str(self.root)!r})"


class NullBackend(Backend):
    def list_keys(self, prefix: str = "") -> set[str]:
        return set()

    def put_file(self, key: str, path: Path) -> None:
        pass

    def put_bytes(self, key: str, data: bytes) -> None:
        pass

    def get_file(self, key: str, path: Path) -> None:
        raise FileNotFoundError(key)

    def get_bytes(self, key: str) -> bytes:
        raise FileNotFoundError(key)

    def exists(self, key: str) -> bool:
        return False


def make_backend(config) -> Backend:
    if config.backend == "null":
        return NullBackend()
    if config.backend == "dir":
        if not config.backup_dir:
            raise ValueError("BACKUP_DIR is required for the dir backend")
        return DirBackend(config.backup_dir)
    if config.backend != "s3":
        raise ValueError(f"unknown backend {config.backend!r}")
    if not config.s3_bucket:
        if getattr(config, "dry_run", False):
            return NullBackend()
        raise ValueError("S3_BUCKET is required for the s3 backend")
    return S3Backend(
        bucket=config.s3_bucket,
        prefix=config.s3_prefix,
        region=config.aws_region,
        endpoint_url=config.s3_endpoint_url,
        storage_class=config.s3_storage_class,
        sse=config.s3_sse,
        kms_key_id=config.s3_kms_key_id,
    )
