"""Where file bytes live: the hub's own disk.

The adapter has one small protocol (``put``, ``read``, ``size``, ``delete``,
``exists``), so another storage (S3) can return as its own decision. Object
keys are random and never reused, so a stored object is never overwritten: a
file's bytes are immutable once published.
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


def for_settings(hub_dir: Path, storage: dict):
    if storage["adapter"] != "local":
        raise StorageError("storage_unavailable", "Only local storage is read in this release.")
    from ..control import deploy

    from .. import datadir

    base = (deploy.state_root(Path(hub_dir)) if storage.get("local_root", "").startswith(".hubzoid/")
            else datadir.root(Path(hub_dir)))
    return LocalStorage(base / storage["local_root"] / (storage.get("prefix") or "hubzoid"))


def new_key() -> str:
    token = secrets.token_hex(16)
    return f"{token[:2]}/{token}"
