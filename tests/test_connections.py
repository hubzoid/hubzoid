"""Tests for hubzoid.connections — the per-user connection helper.

`require(app)` is the whole feature for wrapped tools: it checks the hub's
sanctioned list, reads the per-request identity, and either returns that user's
stored credential or raises NeedsConnection carrying a connect link. The two
security-critical behaviours are: fail closed for an unsanctioned app (never
touch the broker) and never hand one user another user's credential.

`surfaced` turns a raised outcome into a chat message from inside the tool body,
so it works even under the access guard. `ComposioBroker` maps the two calls to
the real SDK. `build`/`attach` wire a Connections from hub settings.
"""
from __future__ import annotations

import asyncio
import types

import pytest
from agents import function_tool
from agents.tool_context import ToolContext

from hubzoid import connections as connlib
from hubzoid import settings as settingslib
from hubzoid.access import Identity, guard, identity_scope
from hubzoid.connections import (
    ComposioBroker,
    Connections,
    ConnectionUnavailable,
    NeedsConnection,
    surfaced,
)


class FakeBroker:
    """Stand-in for the Composio-backed client. Records every call it receives."""

    def __init__(self, creds=None, link_tpl="https://connect.test/{app}?u={user}",
                 connected=None, results=None):
        self._creds = dict(creds or {})
        self._link_tpl = link_tpl
        # Who is connected (for the execute/"via Composio" path, where we check
        # connection status without reading the credential). Defaults to whoever
        # has a stored credential.
        self._connected = set(connected) if connected is not None else set(self._creds)
        self._results = dict(results or {})  # action -> data returned by execute
        self.calls: list[tuple] = []

    def get_credential(self, *, user, app):
        self.calls.append(("get", user, app))
        return self._creds.get((user, app))

    def connect_link(self, *, user, app):
        self.calls.append(("link", user, app))
        return self._link_tpl.format(app=app, user=user)

    def is_connected(self, *, user, app):
        self.calls.append(("is_connected", user, app))
        return (user, app) in self._connected

    def execute(self, *, user, action, arguments):
        self.calls.append(("execute", user, action))
        return self._results.get(action, {})


def _owui(user):
    return identity_scope(Identity.make(user, surface="owui"))


def _invoke_tool(tool) -> str:
    args = "{}"
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


@pytest.fixture(autouse=True)
def _reset_module_gate():
    # attach() arms a process-level gate; reset it so tests don't leak into each
    # other (a bridge process only ever has one hub, but the suite has many).
    yield
    connlib.set_gate(Connections(client=None, allowed=()))


# ---------------------------------------------------------------------------
# require: the gate
# ---------------------------------------------------------------------------
def test_require_returns_stored_credential_for_the_current_user():
    broker = FakeBroker(creds={("priya@x.com", "odoo"): {"login": "priya", "password": "p"}})
    conns = Connections(client=broker, allowed=["odoo"])
    with _owui("priya@x.com"):
        assert conns.require("odoo") == {"login": "priya", "password": "p"}
    assert ("get", "priya@x.com", "odoo") in broker.calls


def test_require_raises_needs_connection_with_link_when_not_connected():
    broker = FakeBroker(creds={})  # nobody connected yet
    conns = Connections(client=broker, allowed=["odoo"])
    with _owui("priya@x.com"):
        with pytest.raises(NeedsConnection) as ei:
            conns.require("odoo")
    err = ei.value
    assert err.app == "odoo"
    assert err.link == "https://connect.test/odoo?u=priya@x.com"
    assert "odoo" in err.tool_message and err.link in err.tool_message


def test_require_fails_closed_for_unsanctioned_app_without_touching_broker():
    broker = FakeBroker(creds={("priya@x.com", "stripe"): {"k": "v"}})
    conns = Connections(client=broker, allowed=["odoo"])  # stripe NOT allowed
    with _owui("priya@x.com"):
        with pytest.raises(ConnectionUnavailable) as ei:
            conns.require("stripe")
    assert ei.value.reason == "not-sanctioned"
    assert broker.calls == []  # broker never asked, even though it held creds


def test_require_denies_anonymous_without_touching_broker():
    broker = FakeBroker()
    conns = Connections(client=broker, allowed=["odoo"])
    with pytest.raises(ConnectionUnavailable) as ei:  # no identity bound
        conns.require("odoo")
    assert ei.value.reason == "anonymous"
    assert broker.calls == []


def test_require_is_per_user_never_returns_another_users_credential():
    broker = FakeBroker(creds={("priya@x.com", "odoo"): {"login": "priya"}})
    conns = Connections(client=broker, allowed=["odoo"])
    with _owui("priya@x.com"):
        assert conns.require("odoo") == {"login": "priya"}
    # Anjali is not connected, and must never receive Priya's credential.
    with _owui("anjali@x.com"):
        with pytest.raises(NeedsConnection):
            conns.require("odoo")
    assert ("get", "anjali@x.com", "odoo") in broker.calls


def test_require_normalizes_the_app_name():
    broker = FakeBroker(creds={("priya@x.com", "odoo"): {"ok": 1}})
    conns = Connections(client=broker, allowed=["Odoo"])  # sanctioned with odd case
    with _owui("priya@x.com"):
        assert conns.require("  ODOO ") == {"ok": 1}  # matches through normalize


def test_require_normalizes_the_user_for_the_vault_key():
    # The vault key must be one canonical email across surfaces: Slack may
    # forward "Priya@X.com" where OWUI signed her up as "priya@x.com".
    broker = FakeBroker(creds={("priya@x.com", "odoo"): {"ok": 1}})
    conns = Connections(client=broker, allowed=["odoo"])
    with _owui(" Priya@X.com "):
        assert conns.require("odoo") == {"ok": 1}
    assert ("get", "priya@x.com", "odoo") in broker.calls


def test_require_fails_closed_when_no_broker_is_configured():
    # A gate with sanctioned apps but no client must fail closed, not crash
    # with AttributeError, if anything ever constructs that combination.
    conns = Connections(client=None, allowed=["odoo"])
    with _owui("p@x.com"):
        with pytest.raises(ConnectionUnavailable) as ei:
            conns.require("odoo")
    assert ei.value.reason == "unconfigured"


def test_require_fails_closed_on_empty_credential_from_broker():
    # Belt and braces at the gate: an empty dict is never a usable credential,
    # whatever broker produced it.
    broker = FakeBroker(creds={("p@x.com", "odoo"): {}})
    conns = Connections(client=broker, allowed=["odoo"])
    with _owui("p@x.com"):
        with pytest.raises(ConnectionUnavailable) as ei:
            conns.require("odoo")
    assert ei.value.reason == "empty-credential"


def test_require_caches_the_credential_briefly():
    # require() sits on the tool hot path: an agent turn making several
    # connected-tool calls must not re-fetch a credential that cannot have
    # changed mid-turn. Short TTL; expiry re-fetches.
    clock = [0.0]
    broker = FakeBroker(creds={("p@x.com", "odoo"): {"ok": 1}})
    conns = Connections(client=broker, allowed=["odoo"], now=lambda: clock[0])
    with _owui("p@x.com"):
        assert conns.require("odoo") == {"ok": 1}
        assert conns.require("odoo") == {"ok": 1}
    gets = [c for c in broker.calls if c[0] == "get"]
    assert len(gets) == 1  # second call served from cache
    clock[0] = 120.0  # beyond the TTL
    with _owui("p@x.com"):
        assert conns.require("odoo") == {"ok": 1}
    assert len([c for c in broker.calls if c[0] == "get"]) == 2


def test_credential_cache_is_per_user():
    broker = FakeBroker(creds={("a@x.com", "odoo"): {"who": "a"},
                               ("b@x.com", "odoo"): {"who": "b"}})
    conns = Connections(client=broker, allowed=["odoo"])
    with _owui("a@x.com"):
        assert conns.require("odoo") == {"who": "a"}
    with _owui("b@x.com"):
        assert conns.require("odoo") == {"who": "b"}  # never a's cached cred


def test_pending_connect_link_is_reused_across_attempts():
    # Every mint creates server-side state at the broker and a different URL;
    # a model retrying tools in one turn must hand the user ONE link. The miss
    # still re-checks the credential so a completed connect is picked up.
    broker = FakeBroker()  # nobody connected
    conns = Connections(client=broker, allowed=["odoo"])
    with _owui("p@x.com"):
        with pytest.raises(NeedsConnection) as e1:
            conns.require("odoo")
        with pytest.raises(NeedsConnection) as e2:
            conns.require("odoo")
    assert e1.value.link == e2.value.link
    assert len([c for c in broker.calls if c[0] == "link"]) == 1
    assert len([c for c in broker.calls if c[0] == "get"]) == 2


# ---------------------------------------------------------------------------
# execute: the "via Composio" path — Composio runs the action, no raw token
# ---------------------------------------------------------------------------
def test_execute_runs_action_for_a_connected_user():
    broker = FakeBroker(creds={("p@x.com", "github"): {"access_token": "t"}},
                        results={"GITHUB_ME": {"login": "octocat"}})
    conns = Connections(client=broker, allowed=["github"])
    with _owui("p@x.com"):
        assert conns.execute("github", "GITHUB_ME", {}) == {"login": "octocat"}
    assert ("execute", "p@x.com", "GITHUB_ME") in broker.calls


def test_execute_raises_needs_connection_when_not_connected():
    broker = FakeBroker(creds={})  # nobody connected
    conns = Connections(client=broker, allowed=["github"])
    with _owui("p@x.com"):
        with pytest.raises(NeedsConnection) as ei:
            conns.execute("github", "GITHUB_ME", {})
    assert ei.value.link == "https://connect.test/github?u=p@x.com"


def test_execute_fails_closed_for_unsanctioned_app_without_touching_broker():
    broker = FakeBroker()
    conns = Connections(client=broker, allowed=["github"])
    with _owui("p@x.com"):
        with pytest.raises(ConnectionUnavailable) as ei:
            conns.execute("stripe", "X", {})
    assert ei.value.reason == "not-sanctioned"
    assert broker.calls == []


def test_execute_denies_anonymous():
    broker = FakeBroker()
    conns = Connections(client=broker, allowed=["github"])
    with pytest.raises(ConnectionUnavailable) as ei:
        conns.execute("github", "X", {})
    assert ei.value.reason == "anonymous"


def test_execute_normalizes_the_user():
    broker = FakeBroker(connected={("p@x.com", "github")}, results={"A": {"ok": 1}})
    conns = Connections(client=broker, allowed=["github"])
    with _owui(" P@X.com "):
        assert conns.execute("github", "A") == {"ok": 1}
    assert ("is_connected", "p@x.com", "github") in broker.calls
    assert ("execute", "p@x.com", "A") in broker.calls


def test_module_execute_uses_the_set_gate():
    broker = FakeBroker(connected={("p@x.com", "github")}, results={"A": {"ok": 2}})
    connlib.set_gate(Connections(client=broker, allowed=["github"]))
    with _owui("p@x.com"):
        assert connlib.execute("github", "A") == {"ok": 2}


def test_broker_is_connected_reflects_active_accounts():
    assert ComposioBroker(FakeComposio(accounts=[{"id": "a", "status": "ACTIVE"}])
                          ).is_connected(user="p@x.com", app="github") is True
    assert ComposioBroker(FakeComposio(accounts=[])
                          ).is_connected(user="p@x.com", app="github") is False


def test_broker_execute_returns_data_and_passes_user_action():
    sdk = FakeComposio(exec_result={"successful": True, "data": {"login": "octocat"}})
    out = ComposioBroker(sdk).execute(user="p@x.com", action="GITHUB_ME", arguments={"a": 1})
    assert out == {"login": "octocat"}
    assert ("tools.execute", "p@x.com", "GITHUB_ME") in sdk.calls


def test_broker_execute_raises_on_unsuccessful_result():
    sdk = FakeComposio(exec_result={"successful": False, "error": "boom", "data": None})
    with pytest.raises(RuntimeError):
        ComposioBroker(sdk).execute(user="p@x.com", action="X", arguments={})


# ---------------------------------------------------------------------------
# surfaced: a raised outcome becomes a chat message, inside the tool body
# ---------------------------------------------------------------------------
def test_surfaced_returns_needs_connection_message():
    @surfaced
    def f():
        raise NeedsConnection("odoo", "https://c/odoo")

    out = f()
    assert "https://c/odoo" in out and "odoo" in out


def test_surfaced_returns_unavailable_message():
    @surfaced
    def f():
        raise ConnectionUnavailable("stripe", "not-sanctioned")

    assert "not available" in f().lower()


def test_surfaced_passes_through_success_and_args():
    @surfaced
    def f(x):
        return f"ok:{x}"

    assert f(3) == "ok:3"


def test_surfaced_reraises_non_connection_errors():
    @surfaced
    def f():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        f()


def test_surfaced_supports_async_functions():
    @surfaced
    async def f():
        raise NeedsConnection("odoo", "ASYNCLINK")

    assert "ASYNCLINK" in asyncio.run(f())


def test_surfaced_misapplied_above_function_tool_fails_loudly():
    # Wrong decorator order (@surfaced ABOVE @function_tool) would silently
    # wrap the FunctionTool in a plain function, which the tool loaders'
    # isinstance scans then skip — the tool would vanish from the registry
    # with zero diagnostics. Fail loudly at import time instead.
    @function_tool
    def some_tool() -> str:
        "d"
        return "x"

    with pytest.raises(TypeError):
        surfaced(some_tool)


def test_surfaced_tool_surfaces_link_even_under_the_access_guard(tmp_path):
    # The real ERPHub shape: a restricted (guarded) tool that needs a connection.
    @function_tool
    @surfaced
    def odoo_read() -> str:
        "restricted tool that needs the user's odoo connection"
        raise NeedsConnection("odoo", "REALLINK")

    guarded = guard.guard_tool(odoo_read, "erp", tmp_path)
    with identity_scope(Identity.make("p", ["erp"], surface="owui")):
        out = _invoke_tool(guarded)
    assert "REALLINK" in out  # the link survives the guard layer


# ---------------------------------------------------------------------------
# ComposioBroker: maps the Composio SDK to the two-call Broker protocol
# ---------------------------------------------------------------------------
class _FakeConnectedAccounts:
    def __init__(self, outer):
        self._o = outer

    def list(self, *, user_ids, toolkit_slugs, statuses):
        self._o.calls.append(("list", tuple(user_ids), tuple(toolkit_slugs), tuple(statuses)))
        return {"items": list(self._o.accounts)}

    def get(self, nanoid):
        self._o.calls.append(("get", nanoid))
        return self._o.detail


class _FakeLink:
    """The real hosted-link resource lives on the LOW-LEVEL client:
    ``composio.client.link.create(auth_config_id=, user_id=)`` -> redirect_url."""

    def __init__(self, outer):
        self._o = outer

    def create(self, *, auth_config_id, user_id, **kwargs):
        self._o.calls.append(("link.create", user_id, auth_config_id))
        return types.SimpleNamespace(
            connected_account_id="ca_new", redirect_url=self._o.redirect)


class _FakeLowClient:
    """Stands in for composio.Composio.client (the HttpClient)."""

    def __init__(self, outer):
        self.link = _FakeLink(outer)


class _FakeTools:
    """Stands in for composio.Composio.tools (the "via Composio" execute path)."""

    def __init__(self, outer):
        self._o = outer

    def execute(self, action, *, user_id, arguments):
        self._o.calls.append(("tools.execute", user_id, action))
        return self._o.exec_result


class _FakeAuthConfigs:
    def __init__(self, outer):
        self._o = outer

    def list(self, *, toolkit_slug):
        self._o.calls.append(("auth_configs.list", toolkit_slug))
        return {"items": list(self._o.auth_config_items)}

    def create(self, toolkit, options):
        # High-level AuthConfigs.create(toolkit_slug, options) -> AuthConfig
        self._o.calls.append(("auth_configs.create", toolkit, options["type"]))
        if self._o.managed_auth_unavailable:
            # Mirrors real Composio for self-hosted toolkits (e.g. Odoo): no
            # managed credentials, so creating a managed auth config 400s.
            raise RuntimeError("Composio has no managed credentials for this toolkit")
        return types.SimpleNamespace(id="ac_created")


class FakeComposio:
    """A minimal stand-in for composio.Composio, dict-like responses and all."""

    def __init__(self, accounts=(), detail=None,
                 redirect="https://composio/connect/redir",
                 auth_configs=({"id": "ac_1", "created_at": "2026-01-01"},),
                 managed_auth_unavailable=False,
                 exec_result=None):
        self.accounts = accounts
        self.detail = detail
        self.redirect = redirect
        self.auth_config_items = list(auth_configs)
        self.managed_auth_unavailable = managed_auth_unavailable
        self.exec_result = exec_result or {"successful": True, "data": {}}
        self.calls: list[tuple] = []
        self.connected_accounts = _FakeConnectedAccounts(self)
        self.auth_configs = _FakeAuthConfigs(self)
        self.client = _FakeLowClient(self)  # composio.Composio.client (HttpClient)
        self.tools = _FakeTools(self)


def test_broker_get_credential_returns_none_when_no_active_account():
    sdk = FakeComposio(accounts=[])
    broker = ComposioBroker(sdk)
    assert broker.get_credential(user="priya@x.com", app="odoo") is None
    # queried filtered to this user, this app, ACTIVE only
    assert ("list", ("priya@x.com",), ("odoo",), ("ACTIVE",)) in sdk.calls


def test_broker_get_credential_returns_state_val_of_active_account():
    sdk = FakeComposio(
        accounts=[{"id": "acc_1", "status": "ACTIVE"}],
        detail={"id": "acc_1",
                "state": {"auth_scheme": "BASIC",
                          "val": {"username": "priya", "password": "p"}}},
    )
    cred = ComposioBroker(sdk).get_credential(user="priya@x.com", app="odoo")
    assert cred == {"username": "priya", "password": "p"}
    assert ("get", "acc_1") in sdk.calls


def test_broker_reads_credential_from_list_item_without_second_fetch():
    # The list response already carries state.val; a second HTTPS round trip
    # per tool call to re-fetch the same record is pure waste.
    sdk = FakeComposio(
        accounts=[{"id": "acc_1", "status": "ACTIVE",
                   "state": {"auth_scheme": "BASIC", "val": {"username": "p"}}}],
    )
    cred = ComposioBroker(sdk).get_credential(user="priya@x.com", app="odoo")
    assert cred == {"username": "p"}
    assert not any(c[0] == "get" for c in sdk.calls)  # no detail re-fetch


def test_broker_drops_none_fields_from_credential():
    # The SDK's state.val is a pydantic model whose dict() carries every
    # optional schema field as None; tools should see only the real values.
    sdk = FakeComposio(
        accounts=[{"id": "acc_1", "status": "ACTIVE"}],
        detail={"id": "acc_1",
                "state": {"auth_scheme": "BASIC",
                          "val": {"username": "priya", "password": "p",
                                  "api_key": None, "subdomain": None}}},
    )
    cred = ComposioBroker(sdk).get_credential(user="priya@x.com", app="odoo")
    assert cred == {"username": "priya", "password": "p"}


def test_broker_connect_link_uses_hosted_link_with_existing_auth_config():
    # toolkits.authorize has no redirect URL for API-key/BASIC toolkits (Odoo).
    # The hosted Connect Link (client.link.create) works for every auth scheme:
    # resolve the toolkit's auth config, then mint the link against it.
    sdk = FakeComposio(redirect="https://composio/connect/abc")
    link = ComposioBroker(sdk).connect_link(user="priya@x.com", app="github")
    assert link == "https://composio/connect/abc"
    assert ("auth_configs.list", "github") in sdk.calls
    assert ("link.create", "priya@x.com", "ac_1") in sdk.calls


def test_broker_connect_link_creates_managed_auth_config_when_none_exists():
    sdk = FakeComposio(redirect="https://composio/connect/xyz", auth_configs=())
    link = ComposioBroker(sdk).connect_link(user="priya@x.com", app="github")
    assert link == "https://composio/connect/xyz"
    assert ("auth_configs.create", "github", "use_composio_managed_auth") in sdk.calls
    assert ("link.create", "priya@x.com", "ac_created") in sdk.calls


def test_broker_connect_link_fails_closed_when_no_managed_auth(caplog):
    # A self-hosted toolkit (e.g. Odoo) with no auth config yet and no managed
    # credentials: fail closed with an operator note, not a raw SDK 400.
    import logging

    sdk = FakeComposio(auth_configs=(), managed_auth_unavailable=True)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ConnectionUnavailable) as ei:
            ComposioBroker(sdk).connect_link(user="priya@x.com", app="odoo")
    assert ei.value.reason == "no-auth-config"
    assert any("auth config" in r.message.lower() for r in caplog.records)


def test_broker_empty_state_val_fails_closed_not_empty_creds(caplog):
    # Composio's DEFAULT project config masks secrets, so an ACTIVE account
    # comes back with no usable state.val. That must NOT be handed to a tool
    # as an empty credential (cryptic downstream failure) — it fails closed
    # with an operator-facing warning naming the masking setting.
    import logging

    sdk = FakeComposio(
        accounts=[{"id": "acc_1", "status": "ACTIVE"}],
        detail={"id": "acc_1", "state": {"auth_scheme": "BASIC", "val": None}},
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ConnectionUnavailable) as ei:
            ComposioBroker(sdk).get_credential(user="priya@x.com", app="odoo")
    assert ei.value.reason == "empty-credential"
    assert any("masking" in r.message.lower() for r in caplog.records)


def test_broker_masked_credential_fails_closed(caplog):
    # Composio secret masking (ON by default) returns the literal string
    # "REDACTED" for secret fields. That is non-empty, so it slips past the
    # empty-credential guard and gets sent upstream ("Bearer REDACTED" -> a
    # cryptic 401). Detect the sentinel and fail closed with an operator note.
    import logging

    sdk = FakeComposio(
        accounts=[{"id": "acc_1", "status": "ACTIVE"}],
        detail={"id": "acc_1",
                "state": {"auth_scheme": "OAUTH2",
                          "val": {"access_token": "REDACTED", "token_type": "bearer",
                                  "scope": "repo"}}},
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ConnectionUnavailable) as ei:
            ComposioBroker(sdk).get_credential(user="p@x.com", app="github")
    assert ei.value.reason == "masked-credential"
    assert any("masking" in r.message.lower() for r in caplog.records)


def test_broker_all_null_state_val_fails_closed():
    # The realistic manifestation of Composio secret masking that we CAN detect:
    # the secret fields come back null, so the cleaned credential is empty.
    sdk = FakeComposio(
        accounts=[{"id": "acc_1", "status": "ACTIVE"}],
        detail={"id": "acc_1",
                "state": {"auth_scheme": "BASIC",
                          "val": {"username": None, "password": None}}},
    )
    with pytest.raises(ConnectionUnavailable) as ei:
        ComposioBroker(sdk).get_credential(user="priya@x.com", app="odoo")
    assert ei.value.reason == "empty-credential"


def test_broker_missing_redirect_url_fails_closed_not_blank_link():
    # authorize() without a redirect_url must not surface a dangling
    # "ask again: " message with an empty link.
    sdk = FakeComposio(redirect=None)
    with pytest.raises(ConnectionUnavailable) as ei:
        ComposioBroker(sdk).connect_link(user="priya@x.com", app="odoo")
    assert ei.value.reason == "no-connect-link"


# ---------------------------------------------------------------------------
# settings + build + attach: config to a live (or inert) Connections
# ---------------------------------------------------------------------------
def _clean_env(monkeypatch):
    monkeypatch.delenv("CONNECTIONS", raising=False)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)


def test_settings_parses_composio_key_and_connections(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("COMPOSIO_API_KEY", "sk_test")
    monkeypatch.setenv("CONNECTIONS", "odoo, Slack ,")  # trims, drops blanks, normalizes
    s = settingslib.load(tmp_path)
    assert s.composio_api_key == "sk_test"
    assert s.connections == ("odoo", "slack")


def test_build_is_inactive_without_connections(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    conns = connlib.build(settingslib.load(tmp_path))
    assert conns.active is False
    with _owui("p@x.com"):
        with pytest.raises(ConnectionUnavailable):
            conns.require("odoo")  # nothing sanctioned


def test_build_is_active_with_injected_broker(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CONNECTIONS", "odoo")
    broker = FakeBroker(creds={("p@x.com", "odoo"): {"ok": 1}})
    conns = connlib.build(settingslib.load(tmp_path), broker=broker)
    assert conns.active is True
    with _owui("p@x.com"):
        assert conns.require("odoo") == {"ok": 1}


def test_build_warns_and_stays_inactive_when_key_missing(tmp_path, monkeypatch, caplog):
    import logging

    _clean_env(monkeypatch)
    monkeypatch.setenv("CONNECTIONS", "odoo")  # declared, but no COMPOSIO_API_KEY
    with caplog.at_level(logging.WARNING):
        conns = connlib.build(settingslib.load(tmp_path))
    assert conns.active is False
    assert any("COMPOSIO_API_KEY" in r.message for r in caplog.records)


def test_attach_hangs_active_connections_on_ctx(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CONNECTIONS", "odoo")
    ctx = types.SimpleNamespace(settings=settingslib.load(tmp_path), connections=None)
    conns = connlib.attach(ctx, broker=FakeBroker(creds={("p@x.com", "odoo"): {"ok": 1}}))
    assert ctx.connections is conns and conns.active is True
    with _owui("p@x.com"):
        assert ctx.connections.require("odoo") == {"ok": 1}


def test_attach_hangs_inactive_connections_when_nothing_declared(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    ctx = types.SimpleNamespace(settings=settingslib.load(tmp_path), connections=None)
    conns = connlib.attach(ctx)
    assert ctx.connections is conns and conns.active is False


# ---------------------------------------------------------------------------
# module-level require: for restricted/ tools that load without a HubContext
# ---------------------------------------------------------------------------
def test_module_require_is_inactive_by_default():
    connlib.set_gate(Connections(client=None, allowed=()))
    with _owui("p@x.com"):
        with pytest.raises(ConnectionUnavailable):
            connlib.require("odoo")


def test_module_require_uses_the_set_gate_and_current_identity():
    broker = FakeBroker(creds={("p@x.com", "odoo"): {"ok": 1}})
    connlib.set_gate(Connections(client=broker, allowed=["odoo"]))
    with _owui("p@x.com"):
        assert connlib.require("odoo") == {"ok": 1}
    # a different user on the same gate is not connected
    with _owui("q@x.com"):
        with pytest.raises(NeedsConnection):
            connlib.require("odoo")


def test_attach_also_arms_the_module_gate(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("CONNECTIONS", "odoo")
    ctx = types.SimpleNamespace(settings=settingslib.load(tmp_path), connections=None)
    connlib.attach(ctx, broker=FakeBroker(creds={("p@x.com", "odoo"): {"ok": 2}}))
    with _owui("p@x.com"):
        assert connlib.require("odoo") == {"ok": 2}  # module-level require now works


def test_mcp_registry_build_arms_the_module_gate(tmp_path, monkeypatch):
    # A restricted tool served over MCP calls the module-level require(); the
    # MCP registry builder must arm the gate just like the chat factories, not
    # rely on a chat runtime having been built first in the same process.
    from hubzoid import mcp_server

    _clean_env(monkeypatch)
    monkeypatch.setenv("CONNECTIONS", "odoo")
    monkeypatch.setenv("COMPOSIO_API_KEY", "sk_test")
    connlib.set_gate(Connections(client=None, allowed=()))  # simulate cold process

    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text(
        "---\nname: connbot\ndescription: t\n---\nYou are ConnBot.\n"
    )
    mcp_server.build_registry(hub)

    assert connlib._GATE.active is True  # gate armed by the MCP path


# ---------------------------------------------------------------------------
# End to end: a real hub built by the factory, restricted odoo tool + link
# ---------------------------------------------------------------------------
_RESTRICTED_ODOO = '''
from agents import function_tool
from hubzoid import connections


@function_tool
@connections.surfaced
def odoo_invoices() -> str:
    "Read Odoo invoices for the calling user."
    creds = connections.require("odoo")
    return "connected as " + str(creds.get("username"))
'''


def test_build_agent_wires_connections_and_restricted_tool_surfaces_link(tmp_path, monkeypatch):
    from hubzoid.factory import build_agent

    _clean_env(monkeypatch)
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used-during-build")
    monkeypatch.setenv("CONNECTIONS", "odoo")
    monkeypatch.setenv("COMPOSIO_API_KEY", "sk_test")

    # Nobody connected -> require() raises NeedsConnection carrying this link.
    broker = FakeBroker(creds={})
    monkeypatch.setattr(ComposioBroker, "from_api_key",
                        classmethod(lambda cls, key: broker))

    hub = tmp_path / "hub"
    (hub / "restricted").mkdir(parents=True)
    (hub / "AGENTS.md").write_text(
        "---\nname: connbot\ndescription: t\n---\nYou are ConnBot.\n"
    )
    (hub / "restricted" / "odoo.py").write_text(_RESTRICTED_ODOO)

    agent = build_agent(hub)
    tools = {getattr(t, "name", ""): t for t in agent.tools}
    assert "odoo_invoices" in tools  # restricted tool loaded and gated

    # Authorized caller (in the odoo group) but not connected -> gets the link.
    with identity_scope(Identity.make("priya@x.com", ["odoo"], surface="owui")):
        out = _invoke_tool(tools["odoo_invoices"])
    assert "connect" in out.lower()
    assert "connect.test/odoo" in out  # the broker's connect link, surfaced

    # And a caller without the odoo group is denied by the access guard first.
    with identity_scope(Identity.make("anon@x.com", ["other"], surface="owui")):
        denied = _invoke_tool(tools["odoo_invoices"])
    assert "access denied" in denied.lower()
