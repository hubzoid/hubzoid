"""A person an app vouched for, during one reply.

The AG-UI route checks the token, then ``hand_over`` keeps the caller for the
conversation until the reply it starts picks it up (``take``, in
``chat.runs``). The reply then runs with:

* ``run_identity``: an access identity with the app scope (``Identity.app``);
  access for it comes from the app's limit alone (``evaluator.app_access``);
* ``token_scope``: the person's own app token, in memory for this reply only,
  for the tools the app names in ``token_to``.

A tool gets the token through ``app_token()`` (a hub tool) or as a header on
the app's connector (``connector_headers``). Any other tool gets nothing. The
exact token is scrubbed from tool output (``scrub``).

Nothing here is written to the database or logged. A reply restarted after a
restart, or continued later by the background loop, has no caller here and so
no access: app replies continue only when the person is back with a fresh
token (``agui``).

How others do it: Agno passes token claims to the run (``dependencies_claims``)
and builds per-run MCP headers (``MCPTools(header_provider=...)``); Mastra's MCP
client takes a custom ``fetch`` per caller; eve resolves per-caller connection
auth (``credentialOwner: "user"``).
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from .caller import AppCaller

_HOLD_SECONDS = 120

_pending: dict[str, tuple[float, AppCaller]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class _Live:
    app_id: str
    token: str | None
    token_to: tuple[str, ...]
    header: str


_live: ContextVar[_Live | None] = ContextVar("hubzoid_app_token", default=None)
_tool: ContextVar[str | None] = ContextVar("hubzoid_app_tool", default=None)


def hand_over(conversation_id: str, caller: AppCaller) -> bool:
    """Keep ``caller`` for the next reply in ``conversation_id``. False when
    another request's caller is already waiting there: it is never replaced."""
    now = time.monotonic()
    with _lock:
        for key in [k for k, (t, _) in _pending.items() if now - t > _HOLD_SECONDS]:
            _pending.pop(key, None)
        if conversation_id in _pending:
            return False
        _pending[conversation_id] = (now, caller)
        return True


def take(conversation_id: str, owner: str) -> AppCaller | None:
    """The caller handed over for this conversation's reply, once, when it is
    the conversation's owner."""
    with _lock:
        held = _pending.pop(conversation_id, None)
    if held is None or time.monotonic() - held[0] > _HOLD_SECONDS:
        return None
    caller = held[1]
    return caller if caller.subject == owner else None


def release(conversation_id: str, caller: AppCaller) -> None:
    """Forget ``caller``'s hand-over when its reply never started (only its own)."""
    with _lock:
        held = _pending.get(conversation_id)
        if held is not None and held[1] is caller:
            _pending.pop(conversation_id, None)


def run_identity(caller: AppCaller | None, owner: str, *, record_id: str | None = None):
    """The access identity of an app reply. Without a caller (nobody handed one
    over) it allows nothing: no agents, no tools."""
    from ..access.identity import AppScope, Identity

    if caller is None:
        scope = AppScope(app_id="", agents=frozenset(), tools=frozenset())
    else:
        scope = AppScope(app_id=caller.app_id, agents=frozenset(caller.access.agents),
                         tools=frozenset(caller.access.tools))
    return Identity.make(owner, (), "app", run_identity_id=record_id, app=scope)


@contextmanager
def token_scope(hub_dir, caller: AppCaller | None) -> Iterator[None]:
    """The person's app token for this reply's tools that the app names."""
    live = None
    if caller is not None and caller.app_token:
        from .config import service_for

        try:
            reg = service_for(hub_dir).get(caller.app_id)
        except Exception:  # noqa: BLE001 - no settings: no token for any tool
            reg = None
        if reg is not None and reg.token_to:
            live = _Live(caller.app_id, caller.app_token, reg.token_to, reg.token_header)
    token = _live.set(live)
    try:
        yield
    finally:
        _live.reset(token)


@contextmanager
def tool_scope(name: str) -> Iterator[None]:
    """Which hub tool is running now (set by the per-call check)."""
    token = _tool.set(name)
    try:
        yield
    finally:
        _tool.reset(token)


def app_token() -> str | None:
    """For a hub tool calling the app's API: the person's own app token, when
    this reply is for a person an app vouched for and the app lists this tool
    in ``token_to``. None otherwise. Never log or return it."""
    live, tool = _live.get(), _tool.get()
    if live is None or not live.token or not tool or tool not in live.token_to:
        return None
    return live.token


def connector_headers(connector_id: str) -> dict | None:
    """The header carrying the person's app token to the app's connector, when
    the app lists ``connector:<id>`` in ``token_to``."""
    live = _live.get()
    if live is None or not live.token or f"connector:{connector_id}" not in live.token_to:
        return None
    value = live.token if live.header.lower() != "authorization" else f"Bearer {live.token}"
    return {live.header: value}


def live_token() -> str | None:
    live = _live.get()
    return live.token if live is not None else None


def scrub(value: Any, token: str | None = None) -> Any:
    """``value`` with the person's exact app token replaced, wherever it shows:
    in text, and inside lists and mappings (keys and values)."""
    token = token or live_token()
    if not token:
        return value
    if isinstance(value, str):
        return value.replace(token, "[redacted]") if token in value else value
    if isinstance(value, dict):
        return {scrub(k, token): scrub(v, token) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, token) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub(v, token) for v in value)
    return value


class _ScrubbingRecord(logging.LogRecord):
    """A log record with the reply's app token removed from its message and
    arguments (a tool's exception text logged by a runtime, for example)."""

    def getMessage(self) -> str:  # noqa: N802 - logging's name
        message = super().getMessage()
        token = live_token()
        return message.replace(token, "[redacted]") if token and token in message else message


def _install_log_scrub() -> None:
    factory = logging.getLogRecordFactory()
    if getattr(factory, "hubzoid_scrubs", False):
        return

    def make(*args, **kwargs):
        record = factory(*args, **kwargs)
        token = live_token()
        if token:
            record.__class__ = _ScrubbingRecord
            if record.exc_info and record.exc_info[1] is not None and token in str(record.exc_info[1]):
                record.exc_text = "[a tool error that included the person's app token]"
                record.exc_info = None
        return record

    make.hubzoid_scrubs = True
    logging.setLogRecordFactory(make)


_install_log_scrub()
