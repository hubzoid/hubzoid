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

# Token/cost usage measured for this request. The runtime writes it when the
# backend reports final usage (Claude's ResultMessage, the OpenAI run's usage);
# the bridge drains it after the turn to fill the OpenAI usage envelope (Open
# WebUI's native token column). A dict container mutated in place (like artifacts), so
# the write survives the context copy the SDK makes for tool calls.
_current_usage: ContextVar[dict | None] = ContextVar("hubzoid_usage", default=None)


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


def record_usage(usage: dict) -> None:
    """Record final token/cost usage for this request. Mutates the scope's
    container in place so the value is visible from the bridge that drains it,
    even though the runtime may write it from a copied context."""
    holder = _current_usage.get()
    if holder is None:
        holder = {}
        _current_usage.set(holder)
    holder.clear()
    holder.update(usage or {})


def drain_usage() -> dict:
    """Return usage recorded this request and clear it. Empty when the backend
    reported none (e.g. an older SDK without partial usage)."""
    holder = _current_usage.get()
    if not holder:
        return {}
    out = dict(holder)
    holder.clear()
    return out


@contextmanager
def chat_scope(chat_id: str | None) -> Iterator[None]:
    """Set the chat id (and fresh artifact + usage registries) for a `with` block."""
    token = _current_chat_id.set(chat_id)
    art_token = _current_artifacts.set([])
    usage_token = _current_usage.set({})
    try:
        yield
    finally:
        _current_chat_id.reset(token)
        _current_artifacts.reset(art_token)
        _current_usage.reset(usage_token)
