"""HMAC-signed tokens for artifact / upload URLs.

The bridge's `/v1/chat/completions` endpoint is bearer-auth gated. But
artifact download URLs are clicked from a browser tab, which sends no
Authorization header — so artifact requests need an alternative way to
prove they came from a session that knows the API key.

We embed a short HMAC of `(chat_id, filename)` keyed by the FIRST bridge
API key in the URL itself (``?t=<token>``). Two properties:

  * The token is deterministic — the same chat_id + filename produces the
    same token, so links written into past chat transcripts keep working
    indefinitely. This matters because Open WebUI persists message text
    verbatim; a one-time token would break old links.
  * The token does not expose the api key — only HMACs of paths.

The HMAC is truncated to 16 hex chars (64 bits). That is enough to make
guessing infeasible for a localhost or LAN deployment. Operators who
need stronger guarantees can rotate the bridge key (which invalidates
all old artifact links) or expose the bridge only on loopback.
"""
from __future__ import annotations

import hmac
import os
from hashlib import sha256


def _secret() -> bytes:
    """Pick the HMAC secret.

    Reads the FIRST entry of BRIDGE_API_KEYS (matching settings.first_api_key).
    Falls back to "dev" to match the bridge's own default.
    """
    raw = os.environ.get("BRIDGE_API_KEYS", "dev")
    first = next((k.strip() for k in raw.split(",") if k.strip()), "dev")
    return first.encode("utf-8")


def sign_artifact_path(chat_id: str, filename: str) -> str:
    """Return a 16-char hex token for ``chat_id/filename``."""
    msg = f"{chat_id}/{filename}".encode("utf-8")
    return hmac.new(_secret(), msg, sha256).hexdigest()[:16]


def verify_artifact_token(chat_id: str, filename: str, token: str | None) -> bool:
    """Constant-time check that ``token`` matches the expected HMAC."""
    if not token:
        return False
    expected = sign_artifact_path(chat_id, filename)
    return hmac.compare_digest(expected, token)
