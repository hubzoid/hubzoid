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

# Download links produced this request (e.g. by `write_artifact`). The runtime
# drains these at end of turn and surfaces any the model did not echo itself,
# so the link reaches the user regardless of backend or model. The list is set
# fresh per request by `chat_scope`; tools mutate it in place (append) so the
# record survives the context copy the SDK makes when running tool calls.
_current_artifacts: ContextVar[list | None] = ContextVar("hubzoid_artifacts", default=None)


def get_chat_id() -> str | None:
    return _current_chat_id.get()


def set_chat_id(chat_id: str | None) -> None:
    _current_chat_id.set(chat_id)


def record_artifact(name: str, url: str) -> None:
    """Register a downloadable artifact produced during this request.

    Append-in-place (not reassign) so the entry is visible from the parent
    context that drains it, even though tool calls run in a copied context.
    See `hubzoid.tool_events.format_artifact_footer` for how it is surfaced.
    """
    items = _current_artifacts.get()
    if items is None:
        items = []
        _current_artifacts.set(items)
    items.append({"name": name, "url": url})


def drain_artifacts() -> list:
    """Return artifacts recorded this request and clear the registry."""
    items = _current_artifacts.get()
    if not items:
        return []
    _current_artifacts.set([])
    return list(items)


@contextmanager
def chat_scope(chat_id: str | None) -> Iterator[None]:
    """Set the chat id (and a fresh artifact registry) for a `with` block."""
    token = _current_chat_id.set(chat_id)
    art_token = _current_artifacts.set([])
    try:
        yield
    finally:
        _current_chat_id.reset(token)
        _current_artifacts.reset(art_token)
