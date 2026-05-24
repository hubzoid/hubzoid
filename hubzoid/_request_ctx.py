"""Per-request chat identity, threaded through tools via ContextVar.

The HubContext is built once at process boot and is shared across every
chat. But tools that touch the filesystem (`write_artifact`, `read_upload`)
need to scope writes to a specific chat so two clients' outputs do not
collide.

Hubzoid's bridge (`server.py`) sets `current_chat_id` on each incoming
`/v1/chat/completions` request. Tools read it via `get_chat_id()` and
resolve their per-chat directory from it. If no chat_id has been set
(e.g. CLI `hubzoid test`, unit tests), tools fall back to the process-
boot session_id stored on the HubContext.

ContextVars are async-safe: each request that is awaited concurrently
gets its own snapshot of the variable. This works under FastAPI's
threadpool routing too because FastAPI runs each request in its own
asyncio task.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

# None when no chat is active (CLI calls, unit tests). A short string
# otherwise. The value should be safe to use as a path component — the
# bridge sanitizes incoming ids before set().
_current_chat_id: ContextVar[str | None] = ContextVar("hubzoid_chat_id", default=None)


def get_chat_id() -> str | None:
    return _current_chat_id.get()


def set_chat_id(chat_id: str | None) -> None:
    _current_chat_id.set(chat_id)


@contextmanager
def chat_scope(chat_id: str | None) -> Iterator[None]:
    """Set the chat id for the duration of a `with` block."""
    token = _current_chat_id.set(chat_id)
    try:
        yield
    finally:
        _current_chat_id.reset(token)
