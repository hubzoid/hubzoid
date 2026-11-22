"""Access decisions at their edges (hubzoid.access.evaluator): who may use a
platform capability (deployment-wide ones only for a deployment
administrator, "any agent" ones for someone who manages at least one agent,
agent ones only in an agent they manage); an embedding app's caller is
refused when the apps settings can't be read or the app was removed; an API
app's work stops when its settings can't be read; a credential check that
fails refuses the tool call (fail closed)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hubzoid.access import evaluator
from hubzoid.access.identity import AppScope, Identity
from hubzoid.embed import config as apps_config


class Grants:
    """manage_access per agent ("*" for the whole deployment) and plain permissions."""

    def __init__(self, manages=(), perms=()):
        self.manages, self.perms = set(manages), set(perms)

    def can(self, email, hub, permission):
        if permission == "manage_access":
            return hub in self.manages
        return (hub, permission) in self.perms

    def hubs_for(self, email):
        return sorted(self.manages - {"*"})


@pytest.mark.parametrize("grants,hub,permission,allowed", [
    (Grants(manages={"*"}), None, "chat_config.manage", True),        # deployment administrator: everything
    (Grants(manages={"finance"}), "finance", "chat_config.manage", False),  # deployment-wide: not an agent manager
    (Grants(manages={"finance"}), None, "admin.access", True),        # any agent they manage
    (Grants(), None, "admin.access", False),
    (Grants(manages={"finance"}), "finance", "admin.diagnostics.run", True),
    (Grants(manages={"finance"}), "hr", "admin.diagnostics.run", False),
    (Grants(manages={"finance"}), None, "admin.diagnostics.run", False),   # an agent capability needs the agent
    (Grants(perms={("finance", "ledger")}), "Finance", "ledger", True),     # agent names are normalised
    (Grants(perms={("finance", "ledger")}), None, "ledger", False),
])
def test_who_may_use_a_capability(grants, hub, permission, allowed):
    assert evaluator._grant_allowed(grants, "ana@x.org", hub, permission) is allowed


APP = AppScope("erpnext", frozenset({"finance"}), frozenset({"*"}))


def test_an_app_caller_is_refused_when_its_app_cant_be_checked(tmp_path, monkeypatch):
    hub = tmp_path / "finance"
    hub.mkdir()

    def unreadable(hub_dir):
        raise apps_config.AppsError(500, "invalid", "apps section broken")

    monkeypatch.setattr(apps_config, "service_for", unreadable)
    assert evaluator.app_access(hub, APP, "ledger") == evaluator.Decision(False, "store_error")

    monkeypatch.setattr(apps_config, "service_for", lambda hub_dir: SimpleNamespace(get=lambda app_id: None))
    assert evaluator.app_access(hub, APP, "ledger") == evaluator.Decision(False, "credential_ended")
    ident = Identity.make("erpnext:alice", surface="embed", app=APP)
    assert evaluator.check_run(hub, ident, tool="lookup").reason == "credential_ended"


def test_an_api_apps_work_stops_when_its_settings_cant_be_read(tmp_path, monkeypatch):
    def unreadable(hub_dir):
        raise apps_config.AppsError(500, "invalid", "apps section broken")

    monkeypatch.setattr(apps_config, "service_for", unreadable)
    assert evaluator.credential_live(tmp_path, "app_jwt", "app:ci") is False


def test_a_failing_credential_check_refuses_the_tool_call(tmp_path, monkeypatch):
    hub = tmp_path / "finance"
    hub.mkdir()
    monkeypatch.setattr(evaluator, "authorize", lambda *a, **kw: evaluator.ALLOW)

    def down(*a, **kw):
        raise OSError("database unreachable")

    monkeypatch.setattr(evaluator, "credential_live", down)
    ident = Identity.make("ana@x.org", surface="web", credential=("session", "s-1"))
    assert evaluator.check_run(hub, ident) == evaluator.Decision(False, "store_error")
    monkeypatch.setattr(evaluator, "credential_live", lambda *a, **kw: False)
    assert evaluator.check_run(hub, ident) == evaluator.Decision(False, "credential_ended")
    assert evaluator.check_run(hub, Identity.make("ana@x.org", surface="web")) == evaluator.ALLOW
    assert evaluator.check_run(hub, None) == evaluator.ALLOW          # anonymous: left to the tool's permission
