"""Session + per-chat bookkeeping.

Two scopes of state coexist in Hubzoid:

  * **Session** — one id per `hubzoid run` invocation, shared across CLI
    calls and used as a fallback when no chat id is in scope.
  * **Chat** — one id per Open WebUI / Slack / API conversation. Set by
    the bridge per-request via `_request_ctx.set_chat_id()`. Tools that
    touch the filesystem (write_artifact, read_upload) resolve their
    directory from this id.

Directory layout under the hub:

    <hub>/output/<session_id>/...            (legacy session-scoped writes)
    <hub>/.hubzoid/chats/<chat_id>/artifacts/  (write_artifact destination)
    <hub>/.hubzoid/chats/<chat_id>/uploads/    (read_upload source)

The leading dot keeps chat state out of the way of the agent's source
files, which the operator authors.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

# How long a chat id we accept from the request body. Open WebUI ids are
# uuids; Slack thread_ts is a float-string; we cap at this to keep paths
# sane and stop a hostile caller from inflating directory names.
_CHAT_ID_MAX = 64
_CHAT_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

CHATS_DIRNAME = ".hubzoid/chats"


def make_session_id() -> str:
    """Return a short, sortable session id (timestamp-style)."""
    return os.environ.get("HUBZOID_SESSION_ID") or uuid.uuid4().hex[:12]


def session_output_dir(hub_dir: Path, session_id: str) -> Path:
    p = hub_dir / "output" / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Per-chat scopes.
# ---------------------------------------------------------------------------
def sanitize_chat_id(raw: object) -> str | None:
    """Coerce an arbitrary value to a safe chat-id string, or None.

    Rules:
      - Must be a non-empty string after sanitization.
      - Disallowed characters (anything outside A-Z, a-z, 0-9, ., _, -)
        are replaced with `-`.
      - Capped at _CHAT_ID_MAX characters.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = _CHAT_ID_SAFE.sub("-", s)
    s = s.strip("-._")
    if not s:
        return None
    return s[:_CHAT_ID_MAX]


def chat_root(hub_dir: Path, chat_id: str) -> Path:
    """Root of all per-chat state for `chat_id`."""
    return hub_dir / CHATS_DIRNAME / chat_id


def chat_artifact_dir(hub_dir: Path, chat_id: str) -> Path:
    p = chat_root(hub_dir, chat_id) / "artifacts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def chat_upload_dir(hub_dir: Path, chat_id: str) -> Path:
    p = chat_root(hub_dir, chat_id) / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p
