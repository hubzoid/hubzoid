"""The deployment key: encrypt small secrets at rest and sign short assertions.

One key per deployment, never stored in the database:

  * ``HUBZOID_SECRET_KEY`` (environment): one or more Fernet keys separated by
    commas. The first encrypts and signs; every key decrypts, so a key can be
    rotated by prepending a new one and re-encrypting at leisure.
  * otherwise a key file created on first use with mode 0600: next to the
    gateway's deployment manifest (shared by every bridge of the deployment),
    or ``<hub>/.hubzoid/secret.key`` for a standalone hub.

Back the key up separately from the data. Without it, encrypted credentials
(personal connection tokens, registered client secrets) cannot be read; the rest
of the database is unaffected.

Used by: personal connection tokens and connector client secrets (encrypt),
identity assertions between Hubzoid processes (sign).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_lock = threading.Lock()
_cache: dict[str, list[bytes]] = {}


class SecretKeyError(RuntimeError):
    """The deployment key is missing, malformed or cannot decrypt a value."""


def key_path(hub_dir: Path, env=None) -> Path:
    """Where the key file lives when ``HUBZOID_SECRET_KEY`` is not set."""
    env = os.environ if env is None else env
    manifest = env.get("HUBZOID_DEPLOYMENT")
    pointer = Path(hub_dir) / ".hubzoid" / "deployment.json"
    if not manifest and pointer.exists():
        try:
            manifest = json.loads(pointer.read_text()).get("manifest")
        except (OSError, ValueError):
            manifest = None
    if manifest:
        return Path(manifest).parent / "secret.key"
    return Path(hub_dir) / ".hubzoid" / "secret.key"


def _parse(raw: str) -> list[bytes]:
    keys = [k.strip().encode() for k in raw.split(",") if k.strip()]
    for k in keys:
        try:
            Fernet(k)
        except (ValueError, TypeError) as exc:
            raise SecretKeyError(
                "HUBZOID_SECRET_KEY must hold Fernet keys (urlsafe base64, 32 bytes) "
                "separated by commas"
            ) from exc
    return keys


def keys(hub_dir: Path, env=None) -> list[bytes]:
    """The deployment's keys, primary first. Creates the key file if needed."""
    env = os.environ if env is None else env
    raw = (env.get("HUBZOID_SECRET_KEY") or "").strip()
    if raw:
        return _parse(raw)
    path = key_path(hub_dir, env)
    cache_key = str(path.resolve()) if path.exists() else str(path)
    with _lock:
        cached = _cache.get(cache_key)
        if cached:
            return cached
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a private temp file, then link it into place: a second
            # process racing us either wins the link (we read its key) or loses
            # it; nobody ever reads a half-written file.
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, Fernet.generate_key() + b"\n")
            finally:
                os.close(fd)
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            finally:
                tmp.unlink(missing_ok=True)
        parsed = _parse(path.read_text())
        if not parsed:
            raise SecretKeyError(f"{path} is empty")
        _cache[str(path.resolve())] = parsed
        return parsed


def _fernet(hub_dir: Path, env=None) -> MultiFernet:
    return MultiFernet([Fernet(k) for k in keys(hub_dir, env)])


def encrypt(hub_dir: Path, data: str | bytes, env=None) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return _fernet(hub_dir, env).encrypt(raw).decode()


def decrypt(hub_dir: Path, token: str, env=None) -> bytes:
    try:
        return _fernet(hub_dir, env).decrypt(token.encode())
    except InvalidToken as exc:
        raise SecretKeyError(
            "A stored secret cannot be decrypted with the deployment key. "
            "Was HUBZOID_SECRET_KEY or the key file replaced?"
        ) from exc


def decrypt_text(hub_dir: Path, token: str, env=None) -> str:
    return decrypt(hub_dir, token, env).decode()


def encrypt_json(hub_dir: Path, value, env=None) -> str:
    return encrypt(hub_dir, json.dumps(value, separators=(",", ":")), env)


def decrypt_json(hub_dir: Path, token: str, env=None):
    return json.loads(decrypt_text(hub_dir, token, env))


def _sign_key(hub_dir: Path, env=None, *, index: int = 0) -> bytes:
    return hashlib.sha256(b"hubzoid-sign-v1:" + keys(hub_dir, env)[index]).digest()


def sign(hub_dir: Path, message: bytes | str, env=None) -> str:
    """HMAC-SHA256 (hex) of ``message`` under the primary key."""
    raw = message.encode() if isinstance(message, str) else message
    return hmac.new(_sign_key(hub_dir, env), raw, hashlib.sha256).hexdigest()


def verify(hub_dir: Path, message: bytes | str, signature: str, env=None) -> bool:
    """Constant-time check of ``signature`` against every configured key."""
    if not signature:
        return False
    raw = message.encode() if isinstance(message, str) else message
    for i in range(len(keys(hub_dir, env))):
        expected = hmac.new(_sign_key(hub_dir, env, index=i), raw, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            return True
    return False


def fingerprint(hub_dir: Path, env=None) -> str:
    """A short, non-secret identifier of the primary key (for doctor output)."""
    return hashlib.sha256(keys(hub_dir, env)[0]).hexdigest()[:12]


def reset_cache() -> None:
    """Tests only: forget cached key files."""
    with _lock:
        _cache.clear()
