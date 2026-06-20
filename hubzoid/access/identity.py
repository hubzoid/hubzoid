# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
"""Per-request caller identity: who is calling, in which groups, on which surface.

This is the verified claim the access checks read. The bridge (`server.py`) sets
it per request from headers populated by the trusted front (Open WebUI, which
sits in front of the localhost bridge and holds its API key). Tools and the
access guard read it via `current_identity()`.

When nothing is set (CLI, unit tests, scheduled background runs) the identity is
anonymous: no user, no groups. Anonymous is denied every restricted tool, which
is the fail-closed default the whole design rests on.

`normalize` is the single rule for comparing names: a permission, a restricted
file stem, and an Open WebUI group name all match through it, so `Sales`,
`sales`, and `" sales "` are the same key. Case-insensitive, whitespace-trimmed.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterable, Iterator


def normalize(name: str) -> str:
    """Canonical form for matching a permission, file stem, or group name."""
    return (name or "").strip().lower()


@dataclass(frozen=True)
class Identity:
    """An immutable snapshot of the caller for one request.

    `groups` are stored already normalized so membership tests are a plain
    set lookup. Build instances with `Identity.make`, never by hand, so the
    normalization is applied consistently.
    """

    user: str | None = None
    groups: frozenset[str] = field(default_factory=frozenset)
    surface: str = "system"

    @property
    def is_anonymous(self) -> bool:
        return self.user is None

    @staticmethod
    def make(user: str | None, groups: Iterable[str] | None = None,
             surface: str = "system") -> "Identity":
        norm = frozenset(
            g for g in (normalize(x) for x in (groups or [])) if g
        )
        return Identity(
            user=(user or None),
            groups=norm,
            surface=(normalize(surface) or "system"),
        )


# The default every context starts from: nobody, no groups, no surface.
ANONYMOUS = Identity()

_current: ContextVar[Identity] = ContextVar("hubzoid_identity", default=ANONYMOUS)


def current_identity() -> Identity:
    """The caller for the active request, or ANONYMOUS outside a request."""
    return _current.get()


def set_identity(identity: Identity) -> None:
    _current.set(identity or ANONYMOUS)


@contextmanager
def identity_scope(identity: Identity | None) -> Iterator[None]:
    """Bind `identity` for the duration of a `with` block, then restore.

    Mirrors `_request_ctx.chat_scope`: the bridge wraps each request's run in
    one of these so tools and the access guard see the right caller, and
    concurrent requests never see each other's identity (ContextVars are
    per-task).
    """
    token = _current.set(identity or ANONYMOUS)
    try:
        yield
    finally:
        _current.reset(token)
