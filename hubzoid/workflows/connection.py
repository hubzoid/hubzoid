# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""`hub.connection(app, ref=None)`: the run person's own connected account.

A Python workflow step that calls an external system as the person the run acts
as asks for that person's credential here. It goes through the hub's one
connection gate (`hubzoid.connections`), keyed by the run's execution identity,
never the workflow author's and never an administrator's.

  * Several active accounts for the app and no `ref`: refused, naming the choice
    (Hubzoid never guesses which account to act through).
  * `ref` given: it must be one of that person's own active accounts.
  * None, expired or revoked: a clear failure telling the person to reconnect
    from chat. No connect link is put in the run's output, where others with
    access to the run history could open it.

The returned `Credential` is read like a dict but redacts itself when printed
and refuses to be pickled, so returning it from a step (which DBOS would
checkpoint) fails loudly instead of storing a secret. Use it inside the step.

Contract with the connection journey work (Agent X, P2): the gate's
`_journey_gate(app)` (connector capability and surface) and the broker's
`active_account_ids(user=, app=)` are used when present. Without them the
Composio account is checked directly against the person's user id.
"""
from __future__ import annotations

from collections.abc import Mapping


class ConnectionFailed(RuntimeError):
    """No usable connection for the run's person. The message says what to do."""


class Credential(Mapping):
    """A connected account's credential. Read-only mapping; never printable or
    serializable, so it cannot leak into logs or DBOS checkpoints by accident."""

    __slots__ = ("_app", "_ref", "_data")

    def __init__(self, app: str, ref: str | None, data: dict):
        object.__setattr__(self, "_app", app)
        object.__setattr__(self, "_ref", ref)
        object.__setattr__(self, "_data", dict(data))

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __repr__(self) -> str:
        return f"<Credential {self._app}{' ' + self._ref if self._ref else ''} (redacted)>"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("A connection credential cannot be saved or returned from a step. "
                        "Use it inside the step that needs it.")

    def __setattr__(self, name, value):
        raise AttributeError("Credential is read-only")


def credential(app: str, *, subject: str, ref: str | None = None) -> Credential:
    """The credential of `subject`'s own connection for `app` (see module doc)."""
    from .. import connections as conns
    from ..access import Identity, identity_scope, normalize

    key = normalize(app)
    gate = conns._GATE
    subject = normalize(subject)
    who = subject
    reconnect = (f"{who} needs to connect {key} again: ask the agent to connect {key} in "
                 "chat, then run the workflow again.")
    with identity_scope(Identity.make(subject, surface="workflow")):
        if key not in getattr(gate, "_allowed", frozenset()):
            raise ConnectionFailed(f"{key} is not a connection this hub offers (CONNECTIONS).")
        journey_gate = getattr(gate, "_journey_gate", None)
        try:
            if journey_gate is not None:
                journey_gate(key)
        except conns.ConnectionsError as exc:
            raise ConnectionFailed(f"{who} may not use the {key} connection here "
                                   f"({getattr(exc, 'reason', exc)}).") from None
        broker = gate._broker()
        if broker is None:
            raise ConnectionFailed(f"{key} connections are not configured for this hub.")
        listing = getattr(broker, "active_account_ids", None)
        ids = listing(user=subject, app=key) if listing is not None else None
        if ref is None:
            if ids is not None and len(ids) > 1:
                raise ConnectionFailed(
                    f"{who} has {len(ids)} active {key} connections. Pass ref= with the one "
                    "to use, so the workflow never guesses.")
            try:
                data = gate.require(key)
            except conns.NeedsConnection:
                raise ConnectionFailed(reconnect) from None
            except conns.ConnectionUnavailable as exc:
                raise ConnectionFailed(
                    f"The {key} connection for {who} is not usable ({exc.reason}).") from None
            return Credential(key, None, data)
        return Credential(key, ref, _by_ref(broker, key, subject, ref, ids, reconnect))


def _by_ref(broker, app: str, subject: str, ref: str, ids, reconnect: str) -> dict:
    """The credential of one named account, only if it is `subject`'s own."""
    from .. import connections as conns

    if ids is not None and ref not in ids:
        raise ConnectionFailed(f"{ref!r} is not one of {subject}'s active {app} connections.")
    client = getattr(broker, "_client", None)
    if client is None:
        raise ConnectionFailed("This connection broker cannot select an account by ref.")
    try:
        detail = client.connected_accounts.get(ref)
    except Exception:  # noqa: BLE001 — unknown to the broker
        raise ConnectionFailed(f"{ref!r} is not one of {subject}'s active {app} connections.") from None
    owner = conns._field(detail, "user_id")
    status = str(conns._field(detail, "status") or "").upper()
    toolkit = conns._field(conns._field(detail, "toolkit"), "slug")
    if owner is None or str(owner).strip().lower() != subject or (
            toolkit and str(toolkit).lower() != app):
        raise ConnectionFailed(f"{ref!r} is not one of {subject}'s active {app} connections.")
    if status and status != "ACTIVE":
        raise ConnectionFailed(reconnect)
    cred = conns._credential_from_val(conns._field(conns._field(detail, "state") or {}, "val"))
    if not cred or any(str(v).strip().upper() == conns._MASK_SENTINEL for v in cred.values()):
        raise ConnectionFailed(f"The {app} connection {ref!r} has no readable credential "
                               "(turn off secret masking for the Composio project).")
    return cred
