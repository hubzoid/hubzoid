"""Where file bytes live: the hub's own disk, or one S3-compatible bucket.

Both adapters share one small protocol (``put``, ``read``, ``size``,
``delete``, ``exists``). Object keys are random and never reused, so a stored
object is never overwritten: a file's bytes are immutable once published.
The S3 adapter uses boto3 (already a dependency); credentials come from a
reference (``env:NAME`` holding ``ACCESS_KEY_ID:SECRET``, or the standard AWS
environment and workload identity when the reference is empty), never from
the hub's YAML.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


class StorageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LocalStorage:
    kind = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise StorageError("invalid_key", "That object key is not valid.")
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.part")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def read(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        path = self._path(key)
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                return fh.read() if end is None else fh.read(max(0, end - start + 1))
        except FileNotFoundError:
            raise StorageError("object_missing", "The stored bytes of this file are missing.")

    def size(self, key: str) -> int:
        try:
            return self._path(key).stat().st_size
        except FileNotFoundError:
            raise StorageError("object_missing", "The stored bytes of this file are missing.")

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    kind = "s3"

    def __init__(self, *, bucket: str, endpoint: str | None, region: str | None, prefix: str,
                 credential_ref: str | None, addressing_style: str = "auto"):
        import boto3
        from botocore.config import Config

        kwargs: dict = {}
        if credential_ref:
            access, secret = _credentials(credential_ref)
            kwargs.update(aws_access_key_id=access, aws_secret_access_key=secret)
        self.client = boto3.client("s3", endpoint_url=endpoint or None, region_name=region or None,
                                   config=Config(s3={"addressing_style": addressing_style},
                                                 retries={"max_attempts": 3, "mode": "standard"},
                                                 connect_timeout=10, read_timeout=60), **kwargs)
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _call(self, fn, **kw):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return fn(Bucket=self.bucket, **kw)
        except ClientError as exc:
            code = (exc.response.get("Error") or {}).get("Code", "")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise StorageError("object_missing", "The stored bytes of this file are missing.")
            if code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403"):
                raise StorageError("storage_auth_failed", "The storage service refused the credentials.")
            raise StorageError("storage_unavailable", f"The storage service failed ({code or 'error'}).")
        except BotoCoreError as exc:
            raise StorageError("storage_unavailable", f"The storage service is unreachable ({type(exc).__name__}).")

    def put(self, key: str, data: bytes) -> None:
        import base64
        import hashlib

        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        self._call(self.client.put_object, Key=self._key(key), Body=data, ChecksumSHA256=digest)

    def read(self, key: str, start: int = 0, end: int | None = None) -> bytes:
        kw = {}
        if start or end is not None:
            kw["Range"] = f"bytes={start}-{'' if end is None else end}"
        return self._call(self.client.get_object, Key=self._key(key), **kw)["Body"].read()

    def size(self, key: str) -> int:
        return int(self._call(self.client.head_object, Key=self._key(key))["ContentLength"])

    def exists(self, key: str) -> bool:
        try:
            self.size(key)
            return True
        except StorageError as exc:
            if exc.code == "object_missing":
                return False
            raise

    def delete(self, key: str) -> None:
        self._call(self.client.delete_object, Key=self._key(key))


def _credentials(ref: str) -> tuple[str, str]:
    if not ref.startswith("env:"):
        raise StorageError("storage_auth_failed", "Storage credentials are an env: reference in this release.")
    raw = os.environ.get(ref[4:], "")
    access, _, secret = raw.partition(":")
    if not access or not secret:
        raise StorageError("storage_auth_failed", f"{ref[4:]} is not set as ACCESS_KEY_ID:SECRET.")
    return access, secret


def for_settings(hub_dir: Path, storage: dict):
    if storage["adapter"] == "s3":
        return S3Storage(bucket=storage["bucket"], endpoint=storage.get("endpoint"), region=storage.get("region"),
                         prefix=storage.get("prefix") or "", credential_ref=storage.get("credential_ref"),
                         addressing_style=storage.get("s3_addressing_style") or "auto")
    from ..control import deploy

    from .. import datadir

    base = (deploy.state_root(Path(hub_dir)) if storage.get("local_root", "").startswith(".hubzoid/")
            else datadir.root(Path(hub_dir)))
    return LocalStorage(base / storage["local_root"] / (storage.get("prefix") or "hubzoid"))


def new_key() -> str:
    token = secrets.token_hex(16)
    return f"{token[:2]}/{token}"
