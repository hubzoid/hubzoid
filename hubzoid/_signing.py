"""HMAC-signed tokens for artifact download URLs.

Artifact links are clicked from a browser tab, which sends no Authorization
header, so the link itself carries proof that the bridge issued it: an HMAC of
`(chat_id, filename)` in ``?t=<token>``.

The HMAC key is the hub's own artifact secret, never the bridge API key (whose
default, "dev", is public). It comes from HUBZOID_ARTIFACT_SECRET when set
(share it across hosts that serve one hub), otherwise from
``<hub>/.hubzoid/artifact_secret``, generated on first use with mode 0600.
Deleting that file (or changing the env value) invalidates every issued link.

Links never expire by default, because Open WebUI keeps message text verbatim
and old links in past chats should keep working. HUBZOID_ARTIFACT_LINK_TTL
(seconds) makes newly issued links expire: they carry ``&e=<unix time>``, which
is covered by the HMAC. Links issued without an expiry stay valid until the
secret changes.
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from hashlib import sha256
from pathlib import Path

_SECRET_FILE = "artifact_secret"
_cache: dict[str, bytes] = {}
_lock = threading.Lock()


def _hub_dir(hub_dir) -> Path | None:
    if hub_dir is not None:
        return Path(hub_dir)
    env = os.environ.get("HUBZOID_HUB_DIR")
    return Path(env) if env else None


def _secret(hub_dir=None) -> bytes:
    env = (os.environ.get("HUBZOID_ARTIFACT_SECRET") or "").strip()
    if env:
        return env.encode("utf-8")
    hub = _hub_dir(hub_dir)
    if hub is None:
        raise RuntimeError(
            "artifact links need the hub directory (HUBZOID_HUB_DIR) or "
            "HUBZOID_ARTIFACT_SECRET"
        )
    path = hub.resolve() / ".hubzoid" / _SECRET_FILE
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and path.is_file():
            return cached
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process created it first; read theirs
        else:
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        value = path.read_text().strip().encode("utf-8")
        _cache[key] = value
        return value


def _ttl() -> int:
    try:
        return max(0, int(os.environ.get("HUBZOID_ARTIFACT_LINK_TTL", "0") or 0))
    except ValueError:
        return 0


def _mac(chat_id: str, filename: str, expires: int | None, hub_dir) -> str:
    msg = f"{chat_id}/{filename}"
    if expires is not None:
        msg += f"/{expires}"
    return hmac.new(_secret(hub_dir), msg.encode("utf-8"), sha256).hexdigest()[:32]


def artifact_query(chat_id: str, filename: str, *, hub_dir=None) -> str:
    """The query string for a download link: ``t=<token>`` plus ``&e=<expiry>``
    when HUBZOID_ARTIFACT_LINK_TTL is set."""
    ttl = _ttl()
    if not ttl:
        return f"t={_mac(chat_id, filename, None, hub_dir)}"
    expires = int(time.time()) + ttl
    return f"t={_mac(chat_id, filename, expires, hub_dir)}&e={expires}"


def sign_artifact_path(chat_id: str, filename: str, *, hub_dir=None) -> str:
    """The never-expiring token for ``chat_id/filename``."""
    return _mac(chat_id, filename, None, hub_dir)


def verify_artifact_token(
    chat_id: str,
    filename: str,
    token: str | None,
    expires: str | None = None,
    *,
    hub_dir=None,
) -> bool:
    """Constant-time check of a link token (and its expiry, when present)."""
    if not token:
        return False
    exp: int | None = None
    if expires is not None:
        try:
            exp = int(expires)
        except ValueError:
            return False
        if exp < time.time():
            return False
    expected = _mac(chat_id, filename, exp, hub_dir)
    return hmac.compare_digest(expected, token)
