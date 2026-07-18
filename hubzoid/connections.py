"""Per-user tool connections: each user acts under their own credential.

A hub tool that needs to act as the calling user (their Odoo login, their
Slack) calls ``connections.require(app)``. That returns *this* user's stored
credential, or raises :class:`NeedsConnection` carrying a link the user opens
once to connect. Credentials live in the broker (Composio), keyed by the user's
identity; the hub never stores per-user secrets itself.

The decision is deliberately small and fails closed:

  1. the app must be on the hub's sanctioned list, or the broker is never
     touched (an unsanctioned tool cannot reach out on a user's behalf), and
  2. the caller must be a known user; anonymous callers get nothing.

Only then is the broker asked for that user's credential. This mirrors
``access`` (the model is never the gate) one layer out: the code, not the LLM,
decides what may be connected and as whom.

Security note: a connect link is personal — it is minted for the requesting
user's identity, and whoever opens it vaults THEIR service account under that
identity. Links surface in the requester's own chat, which is why that is safe;
never re-post one into a shared channel.
"""
from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Callable, Iterable, Protocol

from .access import current_identity, normalize

log = logging.getLogger("hubzoid.connections")

# How long a resolved credential (or a pending connect link) is reused before
# the broker is asked again. A credential cannot change within one agent turn,
# so a short window collapses the several broker round trips a multi-step turn
# would otherwise make into one, without holding a stale token for long.
_CACHE_TTL_SECONDS = 60.0


class Broker(Protocol):
    """What ``Connections`` needs from a credential broker (e.g. Composio).

    A small surface so a fake in a test, or a different broker later, is a
    drop-in. Everything is keyed by the caller's canonical user (their email).
    Two ways to act as the user:

      * "via" the broker — :meth:`is_connected` + :meth:`execute`: the broker
        runs the action with the user's stored credential, which never leaves
        it. Works even when the credential is masked.
      * "direct" — :meth:`get_credential`: hand the raw credential back so the
        hub calls the target itself. Needs an unmasked credential.
    """

    def get_credential(self, *, user: str, app: str) -> dict | None:
        """This user's stored credential for ``app``, or None if not connected."""

    def connect_link(self, *, user: str, app: str) -> str:
        """A one-time hosted link for this user to connect ``app``."""

    def is_connected(self, *, user: str, app: str) -> bool:
        """Whether this user has an active connection for ``app`` (no secret read)."""

    def execute(self, *, user: str, action: str, arguments: dict) -> dict:
        """Run ``action`` as this user via the broker; return the result data."""


class ConnectionsError(Exception):
    """Base for connection outcomes surfaced to a tool."""

    @property
    def tool_message(self) -> str:
        """User-facing text a tool returns to the model. Overridden per case."""
        return str(self)


class NeedsConnection(ConnectionsError):
    """The user is known and the app is sanctioned, but not yet connected.

    Carries the connect ``link`` so a tool can hand it to the user. Surface it
    as a tool result via :attr:`tool_message`.
    """

    def __init__(self, app: str, link: str):
        self.app = app
        self.link = link
        super().__init__(f"connection required for {app!r}")

    @property
    def tool_message(self) -> str:
        return (
            f"To use {self.app}, connect your account first, then ask again: "
            f"{self.link}"
        )


class ConnectionUnavailable(ConnectionsError):
    """The connection cannot be offered at all: not sanctioned, or no user.

    ``reason`` is a short tag (``not-sanctioned`` | ``anonymous``) for logs and
    the user-facing message. Unlike :class:`NeedsConnection` there is no link,
    because there is nothing the user can do from chat to change it.
    """

    def __init__(self, app: str, reason: str):
        self.app = app
        self.reason = reason
        super().__init__(f"connection {app!r} unavailable: {reason}")

    @property
    def tool_message(self) -> str:
        return f"The {self.app} connection is not available here."


class Connections:
    """Per-hub connection gate over a broker, resolving the per-request user.

    Built once per hub from settings (the sanctioned ``allowed`` list and a
    broker bound to the hub's Composio key). One instance serves every user:
    the caller is read from the per-request identity at ``require`` time, so
    concurrent requests never cross.

    The broker may be supplied directly (``client``, as tests do) or lazily via
    ``client_factory`` — a thunk called once on first use — so a hub that
    declares connections but never actually invokes a connection tool in a given
    process (e.g. a scheduled run) pays neither the Composio import nor the
    client construction.
    """

    def __init__(self, *, client: Broker | None = None,
                 allowed: Iterable[str],
                 client_factory: Callable[[], Broker] | None = None,
                 now: Callable[[], float] | None = None):
        self._client = client
        self._client_factory = client_factory
        self._allowed = frozenset(a for a in (normalize(x) for x in allowed) if a)
        self._now = now or time.monotonic
        # (user, app) -> (credential, expiry) and -> (link, expiry). Reused for
        # a short TTL so one agent turn's repeated tool calls don't re-hit the
        # broker for a value that cannot have changed.
        self._cred_cache: dict[tuple[str, str], tuple[dict, float]] = {}
        self._link_cache: dict[tuple[str, str], tuple[str, float]] = {}

    @property
    def active(self) -> bool:
        """True when the hub sanctioned at least one app (the feature is on)."""
        return bool(self._allowed)

    def _broker(self) -> Broker | None:
        """The broker, materialising a lazily-configured one on first use."""
        if self._client is None and self._client_factory is not None:
            self._client = self._client_factory()
        return self._client

    def require(self, app: str) -> dict:
        """Return the current user's credential for ``app``.

        Raises :class:`ConnectionUnavailable` if the app is not sanctioned, the
        broker is not configured, the caller is anonymous, or the account is
        connected but carries no usable credential (the broker is not touched
        for the first two cases); and :class:`NeedsConnection` if the user
        simply has not connected yet.
        """
        key = normalize(app)
        if key not in self._allowed:
            raise ConnectionUnavailable(key, "not-sanctioned")
        broker = self._broker()
        if broker is None:
            # Sanctioned but no broker: a build without a Composio key. Fail
            # closed rather than dereferencing None.
            raise ConnectionUnavailable(key, "unconfigured")
        ident = current_identity()
        if ident.is_anonymous:
            raise ConnectionUnavailable(key, "anonymous")
        # One canonical user key across surfaces (OWUI 'alice@x', Slack
        # 'Alice@X') so a connection made on one surface is found on the others.
        user = normalize(ident.user)
        ck = (user, key)
        now = self._now()

        hit = self._cred_cache.get(ck)
        if hit is not None and hit[1] > now:
            return hit[0]

        cred = broker.get_credential(user=user, app=key)
        if cred:
            self._cred_cache[ck] = (cred, now + _CACHE_TTL_SECONDS)
            self._link_cache.pop(ck, None)  # connected now; drop any pending link
            return cred
        if cred is not None:
            # Connected but empty: never a usable credential. Fail closed.
            raise ConnectionUnavailable(key, "empty-credential")
        raise self._needs_connection(broker, user, key, now)

    def execute(self, app: str, action: str, arguments: dict | None = None) -> dict:
        """Run ``action`` as the current user via the broker ("via Composio").

        The broker executes with the user's stored credential (which never
        leaves it), so this works even when the credential is masked — the path
        to prefer for actions the broker can perform. Same gate as
        :meth:`require`: fails closed for an unsanctioned app, an unconfigured
        broker, or an anonymous caller, and raises :class:`NeedsConnection` with
        a connect link if the user has not connected yet.
        """
        key = normalize(app)
        if key not in self._allowed:
            raise ConnectionUnavailable(key, "not-sanctioned")
        broker = self._broker()
        if broker is None:
            raise ConnectionUnavailable(key, "unconfigured")
        ident = current_identity()
        if ident.is_anonymous:
            raise ConnectionUnavailable(key, "anonymous")
        user = normalize(ident.user)
        if not broker.is_connected(user=user, app=key):
            raise self._needs_connection(broker, user, key, self._now())
        return broker.execute(user=user, action=action, arguments=arguments or {})

    def _needs_connection(self, broker, user: str, key: str, now: float) -> "NeedsConnection":
        """A :class:`NeedsConnection` for ``key``, reusing a still-fresh pending
        link so a model retrying several tools in one turn shows one link (and
        does not mint a fresh server-side auth request per attempt)."""
        pending = self._link_cache.get((user, key))
        if pending is not None and pending[1] > now:
            return NeedsConnection(key, pending[0])
        link = broker.connect_link(user=user, app=key)
        self._link_cache[(user, key)] = (link, now + _CACHE_TTL_SECONDS)
        return NeedsConnection(key, link)


def surfaced(fn):
    """Decorate a tool function so a raised connection outcome becomes its text.

    Apply it directly under ``@function_tool``::

        @function_tool
        @surfaced
        def odoo_invoices(...):
            creds = connections.require("odoo")   # may raise
            ...

    When the body's :func:`Connections.require` raises, the user sees the
    connect link (or the "not available" note) instead of the SDK's generic
    tool error. It catches *inside* the function body, so it behaves the same
    whether or not the tool is also access-guarded (the guard wraps a layer
    further out and never sees the exception). Any non-connection error
    propagates untouched, keeping the SDK's normal error handling.

    The order matters: ``@surfaced`` must sit *under* ``@function_tool`` so it
    wraps the raw function, not the built tool. Applied the other way round it
    would wrap a ``FunctionTool`` in a plain function that the tool loaders'
    ``isinstance`` scans then silently skip, dropping the tool from the hub with
    no diagnostic. Detect that and fail loudly instead.
    """
    if hasattr(fn, "on_invoke_tool"):
        raise TypeError(
            "@surfaced must be applied under @function_tool (it decorates the "
            "raw function, not the built tool). Swap the decorator order."
        )
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except ConnectionsError as err:
                return err.tool_message

        return _async

    @functools.wraps(fn)
    def _sync(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConnectionsError as err:
            return err.tool_message

    return _sync


def _field(obj, key):
    """Read ``key`` off a Composio response, dict-like TypedDict or object."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# The literal value Composio substitutes for secret fields when project-level
# secret masking is on. A credential carrying it is unusable for wrapped tools.
_MASK_SENTINEL = "REDACTED"


def _credential_from_val(val) -> dict:
    """A clean credential dict from a Composio ``state.val`` (dict or model).

    The SDK's ``val`` is a pydantic model whose serialisation carries every
    optional schema field, most of them ``None``; drop those so a tool sees only
    the real values. An all-``None`` ``val`` (the shape Composio returns when
    secret masking nulls the secret fields) reduces to ``{}`` here, which the
    caller treats as "connected but no usable credential".
    """
    if val is None:
        return {}
    if isinstance(val, dict):
        raw = val
    elif hasattr(val, "model_dump"):
        raw = val.model_dump()
    else:
        raw = dict(val)
    return {k: v for k, v in raw.items() if v is not None}


class ComposioBroker:
    """A :class:`Broker` backed by the Composio SDK, keyed by the caller's id.

    Two calls, mapped to the Composio client:

      * ``get_credential`` lists the user's ACTIVE connected account for the
        toolkit and returns its stored credential (from the list item's
        ``state.val``), or None if the user has not connected. Only ACTIVE
        accounts count, so an expired one re-prompts a connect rather than
        handing back a dead token.
      * ``connect_link`` resolves the toolkit's auth config (creating a managed
        one if none exists) and mints a hosted Composio Connect Link for the
        user. The link works for every auth scheme, including the API-key /
        basic-auth toolkits (e.g. Odoo) that have no OAuth redirect.

    The SDK client is injected so a fake stands in for tests; build the real one
    from the hub's ``COMPOSIO_API_KEY`` with :meth:`from_api_key`.

    Note: ``state.val`` carries the raw credential only when secret masking is
    turned OFF for the Composio project (masking is on by default). With masking
    on, the secret fields come back null, ``get_credential`` sees an empty
    credential and fails closed with an operator-facing warning rather than
    handing a tool a half-populated login. This is a one-time Composio project
    setting for hubs whose tools read credentials directly (the wrapped path).
    """

    def __init__(self, client):
        self._client = client

    @classmethod
    def from_api_key(cls, api_key: str) -> "ComposioBroker":
        # Imported here, not at module load, so the composio import cost is paid
        # only by a process that actually resolves a connection.
        from composio import Composio

        return cls(Composio(api_key=api_key))

    def get_credential(self, *, user: str, app: str) -> dict | None:
        resp = self._client.connected_accounts.list(
            user_ids=[user], toolkit_slugs=[app], statuses=["ACTIVE"],
        )
        items = _field(resp, "items") or []
        if not items:
            return None
        item = items[0]
        # The list item already carries state.val; only re-fetch the full record
        # on the older/edge shape where it does not, so the common path is a
        # single round trip. Tolerate SDKs that do not expose a per-item fetch.
        val = _field(_field(item, "state") or {}, "val")
        if val is None:
            try:
                detail = self._client.connected_accounts.get(_field(item, "id"))
                val = _field(_field(detail, "state") or {}, "val")
            except Exception:  # noqa: BLE001
                val = None
        cred = _credential_from_val(val)
        if any(str(v).strip().upper() == _MASK_SENTINEL for v in cred.values()):
            # Composio secret masking (ON by default) replaces secret values with
            # the literal "REDACTED". That is non-empty, so it would otherwise be
            # handed to a tool and sent upstream ("Bearer REDACTED" -> a cryptic
            # 401). Fail closed and name the setting.
            log.warning(
                "connections: %s credential for %s is masked ('REDACTED'). "
                "Composio secret masking is on; turn OFF 'mask secret keys in "
                "connected account' for the project so wrapped tools can read the "
                "raw credential.", app, user,
            )
            raise ConnectionUnavailable(app, "masked-credential")
        if not cred:
            # Connected, but no usable credential — some maskable schemes null the
            # secret fields rather than redacting them. Handing a tool {} produces
            # a cryptic downstream login failure; fail closed and tell the operator.
            log.warning(
                "connections: ACTIVE %s account for %s has an empty credential "
                "(state.val). Turn OFF secret masking for the Composio project "
                "so wrapped tools can read the raw credential.", app, user,
            )
            raise ConnectionUnavailable(app, "empty-credential")
        return cred

    def _auth_config_id(self, app: str) -> str:
        """The toolkit's auth config id, creating a managed one if none exists.

        Newest existing config for the toolkit wins; otherwise a Composio-managed
        auth config is created on the fly. Managed auth only exists for toolkits
        Composio brokers credentials for (e.g. GitHub). Self-hosted toolkits
        (e.g. Odoo) have no managed auth, so an admin must create the auth config
        in Composio first; we fail closed with an operator-facing note rather
        than surfacing a raw 400.
        """
        configs = self._client.auth_configs.list(toolkit_slug=app)
        items = _field(configs, "items") or []
        if items:
            newest = sorted(
                items, key=lambda c: _field(c, "created_at") or "", reverse=True,
            )[0]
            return _field(newest, "id")
        try:
            created = self._client.auth_configs.create(
                app,
                {
                    "type": "use_composio_managed_auth",
                    "tool_access_config": {"tools_for_connected_account_creation": []},
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "connections: no auth config for %s and Composio-managed auth is "
                "unavailable (%s). Create an auth config for this toolkit in the "
                "Composio dashboard (self-hosted toolkits need connection details).",
                app, exc,
            )
            raise ConnectionUnavailable(app, "no-auth-config")
        return _field(created, "id")

    def connect_link(self, *, user: str, app: str) -> str:
        # The hosted Composio Connect Link works for every auth scheme (unlike
        # toolkits.authorize, which has no redirect for non-OAuth toolkits). It
        # lives on the low-level client: client.link.create(auth_config_id=, user_id=).
        acid = self._auth_config_id(app)
        req = self._client.client.link.create(auth_config_id=acid, user_id=user)
        link = _field(req, "redirect_url")
        if not link:
            # No hosted link — surfacing an empty one would tell the user to
            # "ask again" with nothing to click. Fail closed instead.
            log.warning(
                "connections: connect link for (%s, %s) had no redirect_url; "
                "cannot offer a connect link.", user, app,
            )
            raise ConnectionUnavailable(app, "no-connect-link")
        return link

    def is_connected(self, *, user: str, app: str) -> bool:
        # Just checks for an ACTIVE account — never reads state.val, so it works
        # even for managed-auth toolkits whose credential is always masked.
        resp = self._client.connected_accounts.list(
            user_ids=[user], toolkit_slugs=[app], statuses=["ACTIVE"],
        )
        return bool(_field(resp, "items"))

    def execute(self, *, user: str, action: str, arguments: dict) -> dict:
        # Composio runs the action server-side with the user's credential (which
        # never leaves Composio), so masking is irrelevant. `data` is the action
        # result; a falsy `successful` is a real execution failure.
        resp = self._client.tools.execute(action, user_id=user, arguments=arguments or {})
        if _field(resp, "successful") is False:
            raise RuntimeError(f"{action} failed: {_field(resp, 'error') or 'unknown error'}")
        data = _field(resp, "data")
        return dict(data) if isinstance(data, dict) else (data if data is not None else {})


def build(settings, *, broker: Broker | None = None) -> Connections:
    """Assemble a :class:`Connections` from a hub's settings.

    Returns an inactive gate (every ``require`` fails closed) when the hub
    declared no ``CONNECTIONS``, or declared some but has no ``COMPOSIO_API_KEY``
    (a misconfiguration we warn about rather than crash on). ``broker`` is for
    tests; production builds a :class:`ComposioBroker` from the hub's key.
    """
    allowed = tuple(a for a in (getattr(settings, "connections", ()) or ()) if a)
    if not allowed:
        return Connections(client=broker, allowed=())
    if broker is not None:
        return Connections(client=broker, allowed=allowed)
    key = getattr(settings, "composio_api_key", None)
    if not key:
        log.warning(
            "CONNECTIONS is set (%s) but COMPOSIO_API_KEY is missing; "
            "per-user connections are disabled for this hub.",
            ", ".join(allowed),
        )
        return Connections(client=None, allowed=())
    # Defer building the real broker (and importing composio) until the first
    # connection is actually resolved, so a process that never calls a
    # connection tool pays nothing.
    return Connections(
        allowed=allowed,
        client_factory=lambda: ComposioBroker.from_api_key(key),
    )


def attach(ctx, *, broker: Broker | None = None) -> Connections:
    """Build the hub's :class:`Connections` and hang it on ``ctx.connections``.

    Called once by the factory so every tool reaches the same gate at call time
    via ``ctx.connections.require(...)``. Returns the gate (inactive when the
    hub declared no connections). Side-effect free beyond setting the attribute,
    so it is a no-op for hubs that don't use the feature.
    """
    conns = build(ctx.settings, broker=broker)
    ctx.connections = conns
    set_gate(conns)
    return conns


# ---------------------------------------------------------------------------
# Module-level gate: for tools that load without a HubContext (restricted/).
#
# A bridge process serves exactly one hub and builds its runtime once, so a
# module-level gate set at build time is correct. Tools that DO have a ctx use
# the same object via ctx.connections; restricted/ tools, loaded as bare
# module-level FunctionTools, import and call this require() instead.
# ---------------------------------------------------------------------------
_INACTIVE = Connections(client=None, allowed=())
_GATE: Connections = _INACTIVE


def set_gate(conns: Connections) -> None:
    """Record the hub's gate so the module-level :func:`require` can reach it."""
    global _GATE
    _GATE = conns


def require(app: str) -> dict:
    """Return the current user's credential for ``app`` (no HubContext needed).

    The convenience a restricted tool calls directly::

        from hubzoid import connections
        creds = connections.require("odoo")

    The hub's gate is bound at build time; the caller is read from the
    per-request identity. Raises the same :class:`NeedsConnection` /
    :class:`ConnectionUnavailable` as :meth:`Connections.require`, so pair it
    with :func:`surfaced` on the tool to show the connect link.
    """
    return _GATE.require(app)


def execute(app: str, action: str, arguments: dict | None = None) -> dict:
    """Run ``action`` as the current user via the broker (no HubContext needed).

    The "via Composio" convenience a restricted tool calls directly::

        from hubzoid import connections
        me = connections.execute("github", "GITHUB_GET_THE_AUTHENTICATED_USER")

    Same gate and :class:`NeedsConnection` behaviour as
    :meth:`Connections.execute`; pair it with :func:`surfaced`.
    """
    return _GATE.execute(app, action, arguments)
