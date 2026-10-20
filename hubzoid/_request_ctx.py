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

import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
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

# Tool calls made during this request, recorded ONLY when something is
# listening (the eval runner). Default None means "not recording", so
# `record_tool_call` is a single is-None check on the chat path — evals must
# not cost production anything.
#
# This is recorded at the call site rather than scraped from the streamed text
# because the text form depends on SHOW_TOOLS: with `SHOW_TOOLS=off` a real
# tool call produces no output at all, and an eval asserting `expect_tools`
# would silently fail on a hub that turns tool display off.
_current_tool_calls: ContextVar[list | None] = ContextVar("hubzoid_tool_calls", default=None)

# How this request's agent run failed, if it did: {"kind", "reset_at"} (see
# `hubzoid.agent_errors.notice`). Scheduled tasks and evals read it, because the
# plain sentence a person sees for a usage limit carries no '[agent error:'
# marker. A dict mutated in place, like artifacts, so the runtime's write is
# visible from the caller's context.
_current_failure: ContextVar[dict | None] = ContextVar("hubzoid_run_failure", default=None)
# What each tool call returned, by the runtime's call id, recorded ONLY when the
# eval runner listens (`tool_result_recorder`). The same single is-None check
# on the chat path as `record_tool_call`. Only the start of the payload is kept
# (`RESULT_KEEP_CHARS`): the runner needs a short preview, never the data.
_current_tool_results: ContextVar[dict | None] = ContextVar("hubzoid_tool_results", default=None)

RESULT_KEEP_CHARS = 2000

# Cards a tool presented this request (`hubzoid.apps.cards.present`): each is
# the text the model got plus the card, until the runtime adapter that emits
# the tool's result takes it (`take_card`). A list mutated in place, like
# artifacts, so the tool's write survives the SDK's context copies.
_current_cards: ContextVar[list | None] = ContextVar("hubzoid_cards", default=None)

# One id per chat request (one turn of the conversation), so a tool can tell a
# yes the person typed in a later message from a call the model made in the
# same turn. None outside a chat request (MCP calls, CLI, tests).
_current_turn: ContextVar[str | None] = ContextVar("hubzoid_turn", default=None)


def get_turn_id() -> str | None:
    return _current_turn.get()


# Set by the scheduled runner after resolving the account and its scratch path.
# Context-local rather than a global allowlist: parallel jobs and chats cannot
# borrow one another's access. Store the lexical scope so later symlink changes
# are checked by the file tools rather than silently granting the link target.
_schedule_read_scope: ContextVar[tuple[Path, Path] | None] = ContextVar(
    "hubzoid_schedule_read_scope", default=None,
)


def get_schedule_read_scope() -> tuple[Path, Path] | None:
    return _schedule_read_scope.get()


@contextmanager
def schedule_read_scope(hub_dir: Path, scratch_rel: str) -> Iterator[None]:
    """Allow this scheduled execution to read its own account's working files.

    Runner-owned scope, never a tool argument or a broad writable-path grant.
    File tools still enforce containment, symlink and private-content checks.
    """
    hub = Path(hub_dir).resolve()
    from .datadir import resolve

    token = _schedule_read_scope.set((hub, resolve(hub, scratch_rel)))
    try:
        yield
    finally:
        _schedule_read_scope.reset(token)


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


def note_usage(**fields) -> None:
    """Add fields (e.g. status="error", model=...) to this request's usage
    without replacing what the backend recorded. No-op outside a scope."""
    holder = _current_usage.get()
    if holder is None:
        return
    holder.update(fields)


def drain_usage() -> dict:
    """Return usage recorded this request and clear it. Empty when the backend
    reported none (e.g. an older SDK without partial usage)."""
    holder = _current_usage.get()
    if not holder:
        return {}
    out = dict(holder)
    holder.clear()
    return out


def note_run_failure(kind: str, *, reset_at: str | None = None) -> None:
    """Record that this request's run failed, and its class. No-op outside a scope."""
    holder = _current_failure.get()
    if holder is None:
        return
    holder.clear()
    holder.update(kind=kind, reset_at=reset_at)


def run_failure() -> dict | None:
    """{"kind", "reset_at"} when this request's run failed (as recorded by the
    runtime), else None."""
    holder = _current_failure.get()
    return dict(holder) if holder else None


def record_tool_call(name: str, args: object | None = None) -> None:
    """Note that `name` was called, if a recorder is active. No-op otherwise.

    Append-in-place (not reassign) so the entry is visible from the parent
    context that reads it, even though the backends emit tool events from a
    context the SDK copied. Same reasoning as `record_artifact`.
    """
    items = _current_tool_calls.get()
    if items is None:
        return
    items.append({"name": name, "args": args})


def record_tool_result(call_id: object, output: object) -> None:
    """Note what tool call `call_id` returned, if a result recorder is active.
    No-op otherwise. Mutates the recorder's dict in place, like
    `record_tool_call`, so the entry survives the SDK's context copies."""
    results = _current_tool_results.get()
    if results is None:
        return
    results[str(call_id or "")] = _result_text(output)


def _result_text(output: object, limit: int = RESULT_KEEP_CHARS) -> str:
    """The start of a tool result as text. Runtimes hand over a string, a list of
    content blocks ({"type": "text", "text": ...} or objects with `.text`), or
    any JSON-able value."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output[:limit]
    if isinstance(output, (list, tuple)):
        parts: list[str] = []
        size = 0
        for block in output:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            parts.append(text)
            size += len(text)
            if size >= limit:
                break
        if parts:
            return "\n".join(parts)[:limit]
    try:
        import json

        return json.dumps(output, default=str, ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return str(output)[:limit]


def record_card(text: str, card: dict, *, call_id: str | None = None) -> None:
    """Note that a tool's result ``text`` shows ``card``, for the call
    ``call_id`` when the runtime gave the tool one. No-op outside a scope (a
    CLI call shows no chat)."""
    items = _current_cards.get()
    if items is not None:
        items.append((call_id or "", text, card))


def take_card(output: object, *, call_id: str | None = None, tool: str | None = None) -> dict | None:
    """The card for a tool result the runtime is about to emit: the one
    recorded for ``call_id``, else the oldest one of the same ``tool`` whose
    text is exactly the result's. None when the result shows no card."""
    items = _current_cards.get()
    if not items:
        return None
    if call_id:
        for i, (cid, _text, card) in enumerate(items):
            if cid and cid == call_id:
                del items[i]
                return card
    shown = _result_text(output, limit=10**7)
    for i, (_cid, text, card) in enumerate(items):
        if text == shown and (tool is None or card.get("tool") == tool):
            del items[i]
            return card
    return None


@contextmanager
def card_scope() -> Iterator[list]:
    """A fresh card registry for a block (an MCP tool call)."""
    items: list = []
    token = _current_cards.set(items)
    try:
        yield items
    finally:
        _current_cards.reset(token)


@contextmanager
def tool_result_recorder() -> Iterator[dict]:
    """Record what each tool call returned inside the block, as
    {call id: start of the result text}. Used by the eval runner for result
    previews and failure details; outside such a block nothing is recorded."""
    results: dict = {}
    token = _current_tool_results.set(results)
    try:
        yield results
    finally:
        _current_tool_results.reset(token)


@contextmanager
def tool_call_recorder() -> Iterator[list]:
    """Record every tool call made inside the block into the yielded list.

    Used by the eval runner to check `expect_tools` / `forbid_tools`. Outside
    such a block nothing is recorded and nothing is allocated.
    """
    items: list = []
    token = _current_tool_calls.set(items)
    try:
        yield items
    finally:
        _current_tool_calls.reset(token)


@contextmanager
def chat_scope(chat_id: str | None) -> Iterator[None]:
    """Set the chat id (and fresh artifact, usage and failure registries) for a
    `with` block."""
    token = _current_chat_id.set(chat_id)
    turn_token = _current_turn.set(secrets.token_hex(8))
    art_token = _current_artifacts.set([])
    usage_token = _current_usage.set({})
    failure_token = _current_failure.set({})
    cards_token = _current_cards.set([])
    try:
        yield
    finally:
        _current_cards.reset(cards_token)
        _current_chat_id.reset(token)
        _current_turn.reset(turn_token)
        _current_artifacts.reset(art_token)
        _current_usage.reset(usage_token)
        _current_failure.reset(failure_token)


# -- native runtime sessions ----------------------------------------------------------
class SessionSlot:
    """One chat run's binding to the runtime's own session (Claude session id,
    Codex thread, OpenAI Session). ``resume_id`` and ``delta_prompt`` come in:
    a runtime that can resume sends only the new turn into that session; any
    other runtime ignores the slot and uses the full prompt. ``session_id``
    goes out: the session the run ended in, for the next turn of the branch."""

    def __init__(self, resume_id: str | None = None, delta_prompt: str | None = None):
        self.resume_id = resume_id
        self.delta_prompt = delta_prompt
        self.session_id: str | None = None
        self.resumed = False
        self.runtime: str | None = None
        #: () -> bool: may the run start another tool step (not stopped, still
        #: owning its branch). A runtime with a pre-tool hook refuses the step.
        self.guard = None
        #: this turn may use the runtime's own web search (policy, grant and the
        #: person's choice all agreed); a runtime without one ignores it.
        self.web_search = False
        #: a model the person chose for this turn (from the runtime's own list), or None.
        self.model: str | None = None
        #: when set, the only hub tools this run may call (a comparison candidate reads, never acts).
        self.tool_allowlist: list[str] | None = None


_current_session_slot: ContextVar = ContextVar("hubzoid_session_slot", default=None)


def native_session() -> SessionSlot | None:
    return _current_session_slot.get()


@contextmanager
def native_session_scope(slot: SessionSlot | None) -> Iterator[None]:
    token = _current_session_slot.set(slot)
    try:
        yield
    finally:
        _current_session_slot.reset(token)
