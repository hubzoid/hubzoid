"""The capability catalogue: registration, grouping, configuration status and
the authorization rules the Console relies on.

No model and no network: the two-hub SQLite deployment from
test_access_service, a throwaway registered capability and a fake tool.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from agents.tool import FunctionTool
from agents.tool_context import ToolContext
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid import capabilities, config_secrets, deployment
from hubzoid.access import Identity, identity_scope
from hubzoid.access.guard import guard_tool
from hubzoid.access.service import Denied
from hubzoid.capabilities import Capability
from hubzoid.portal import build_router

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    ROOT,
    actor,
    dep,
)

FAKE_SECRET = "sk-probe-VALUE-must-not-leak-4242"


@pytest.fixture
def register(monkeypatch):
    """Register throwaway capabilities for one test only."""
    monkeypatch.setattr(capabilities, "_registry", dict(capabilities._registry))
    monkeypatch.setattr(config_secrets, "_base_env", None)  # deployment layer = os.environ
    for key in ("HZ_PROBE_KEY", "HZ_PROBE_ON", "AWS_SECRET_NAME"):
        monkeypatch.delenv(key, raising=False)
    return capabilities.register


@pytest.fixture
def api(dep, monkeypatch):
    monkeypatch.setenv("HUBZOID_PORTAL_DEV", "1")
    app = FastAPI()
    app.include_router(build_router(dep.hub_dir))
    client = TestClient(app)

    def as_(subject):
        monkeypatch.setenv("HUBZOID_PORTAL_DEV_USER", subject)
        client.headers.update({"origin": "http://testserver"})
        return client

    dep.as_ = as_
    return dep


def _apply(client, subject, *ops):
    return client.post("/portal/api/access/apply", json=dict(
        subject=subject, hub="finance",
        operations=[dict(action=a, permission=p) for a, p in ops]))


def _by_id(entries):
    return {e["permission"]: e for e in entries}


def _invoke(tool, args: dict) -> str:
    raw = json.dumps(args)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=raw)
    return asyncio.run(tool.on_invoke_tool(ctx, raw))


# ---- catalogue ------------------------------------------------------------------

def test_catalogue_groups_every_kind_and_keeps_the_old_fields(dep):
    entries = dep.svc.catalog("finance")
    by = _by_id(entries)
    assert {p: e["group"] for p, e in by.items()} == {
        "use_hub": "hub", "curator": "tools", "jev": "tools", "share_public_links": "tools",
        "ledger": "restricted", "payroll": "restricted", "manage_access": "admin"}
    # Display order follows the drawer's groups; no empty Workflows group.
    assert [e["permission"] for e in entries] == [
        "use_hub", "curator", "jev", "share_public_links", "ledger", "payroll", "manage_access"]
    for e in entries:
        assert {"permission", "label", "description", "sensitive", "group", "surfaces", "status",
                "available", "default", "delegate_grantable", "obsolete"} <= set(e)
    assert by["curator"]["label"] == "Save shared knowledge"
    assert by["manage_access"]["delegate_grantable"] is False
    # The legacy wrapper returns the same capabilities for migration and the CLI.
    assert [e["permission"] for e in deployment.permission_catalog(dep.dirs["finance"])] == [
        e["permission"] for e in entries]


def test_restricted_discovery_never_runs_the_module(dep, tmp_path):
    marker = tmp_path / "executed"
    (dep.dirs["finance"] / "restricted" / "boom.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\nraise RuntimeError('boom at import')\n")
    boom = _by_id(dep.svc.catalog("finance"))["boom"]
    assert boom["group"] == "restricted" and boom["available"] is True
    assert not marker.exists()


def test_permissions_yaml_labels_restricted_but_cannot_change_builtins(dep, caplog):
    identity = dep.dirs["finance"] / "identity"
    identity.mkdir()
    (identity / "permissions.yaml").write_text(
        "curator:\n  label: Anyone may write\n  sensitive: true\n"
        "manage_access:\n  label: Just chat\n"
        "payroll:\n  label: Run payroll\n  sensitive: true\n")
    with caplog.at_level(logging.WARNING, logger="hubzoid.capabilities"):
        by = _by_id(dep.svc.catalog("finance"))
    assert by["curator"]["label"] == "Save shared knowledge" and not by["curator"]["sensitive"]
    assert by["manage_access"]["label"] == "Manage access"
    assert by["payroll"]["label"] == "Run payroll" and by["payroll"]["sensitive"]
    assert "built-in 'curator'" in caplog.text


# ---- an ordinary registered capability ----------------------------------------------

def test_registered_capability_appears_is_granted_and_enforced_without_bespoke_code(api, register):
    probe = register(Capability(permission="zz_probe", label="Probe tool", group="tools",
                                description="A throwaway capability.", surfaces=("chat",)))
    client = api.as_(ROOT)
    catalog = _by_id(client.get("/portal/api/access", params={"hub": "finance"}).json()["permissions"])
    assert (catalog["zz_probe"]["group"], catalog["zz_probe"]["label"], catalog["zz_probe"]["available"]) == (
        "tools", "Probe tool", True)

    async def run(_ctx, _raw):
        return "ran"

    tool = guard_tool(FunctionTool(name="probe_tool", description="probe", params_json_schema={
        "type": "object", "properties": {}, "additionalProperties": True},
        on_invoke_tool=run, strict_json_schema=False), probe.permission, api.hub_dir)
    ann = Identity.make("ann@x.org", surface="owui")

    # Off by default: hidden and refused, whatever the model's arguments claim.
    with identity_scope(ann):
        assert tool.is_enabled() is False
        assert "access denied" in _invoke(tool, {"user": ROOT, "as": ROOT})
    assert _apply(client, "ann@x.org", ("grant", "zz_probe")).status_code == 200
    with identity_scope(ann):
        assert tool.is_enabled() is True
        assert _invoke(tool, {}) == "ran"
    # Granting it implies entry only, nothing unrelated.
    assert api.gs.permissions_for("ann@x.org", "finance") == {"use_hub", "zz_probe"}
    # Revocation holds on the next call with the same tool instance.
    assert _apply(client, "ann@x.org", ("revoke", "zz_probe")).status_code == 200
    with identity_scope(ann):
        assert tool.is_enabled() is False
        assert "access denied" in _invoke(tool, {})
    with identity_scope(Identity.make(ROOT, surface="slack-channel")):
        assert "access denied" in _invoke(tool, {})  # the surface gate still applies


# ---- configuration status -----------------------------------------------------------

def test_configuration_status_is_by_name_and_never_exposes_values(api, register, monkeypatch, caplog):
    register(Capability(permission="zz_keyed", label="Keyed tool", group="tools",
                        requires=("HZ_PROBE_KEY",), missing="Probe key missing"))
    register(Capability(permission="zz_switched", label="Switched tool", group="tools",
                        enabled_by="HZ_PROBE_ON"))
    hub_env = api.dirs["finance"] / ".env"

    def status(pid):
        e = _by_id(api.svc.catalog("finance"))[pid]
        return e["available"], e["status"]

    assert status("zz_keyed") == (False, "Probe key missing")
    hub_env.write_text("HZ_PROBE_KEY=\n")
    assert status("zz_keyed") == (False, "Probe key missing")  # blank is absent
    hub_env.write_text(f"HZ_PROBE_KEY={FAKE_SECRET}\n")
    assert status("zz_keyed") == (True, "")
    hub_env.write_text("")
    monkeypatch.setenv("HZ_PROBE_KEY", FAKE_SECRET)  # the deployment layer
    assert status("zz_keyed") == (True, "")
    monkeypatch.delenv("HZ_PROBE_KEY")

    # A named hub secret is never fetched here: absent locally means "Not checked".
    def no_fetch(*_a, **_k):
        raise AssertionError("the catalogue must not fetch secrets")

    monkeypatch.setattr(config_secrets, "fetch", no_fetch)
    monkeypatch.setattr(config_secrets, "load_secret", no_fetch)
    hub_env.write_text("HUBZOID_HUB_SECRET_NAME=hub/finance\n")
    assert status("zz_keyed") == (None, "Not checked")

    assert status("zz_switched") == (True, "")
    hub_env.write_text("HZ_PROBE_ON=false\n")
    assert status("zz_switched") == (False, "Disabled for this hub")
    hub_env.write_text(f"HZ_PROBE_ON=true\nHZ_PROBE_KEY={FAKE_SECRET}\n")
    assert status("zz_switched") == (True, "")

    # An unconfigured capability can still be granted; it simply cannot run yet.
    hub_env.write_text("")
    client = api.as_(ROOT)
    assert _apply(client, "ann@x.org", ("grant", "zz_keyed")).status_code == 200
    hub_env.write_text(f"HZ_PROBE_KEY={FAKE_SECRET}\n")
    with caplog.at_level(logging.DEBUG):
        responses = [client.get("/portal/api/access", params={"hub": "finance"}).text,
                     client.get("/portal/api/permissions", params={"hub": "finance"}).text,
                     client.get("/portal/api/me").text]
    assert all(FAKE_SECRET not in r for r in responses)
    assert FAKE_SECRET not in caplog.text


# ---- defaults, delegates and obsolete grants ------------------------------------------

def test_defaults_never_include_a_sensitive_capability(register):
    with pytest.raises(ValueError, match="sensitive"):
        Capability(permission="zz_bad", label="Bad", group="tools", sensitive=True, default="included")
    with pytest.raises(ValueError):
        Capability(permission="zz_bad", label="Bad", group="settings")
    register(Capability(permission="zz_once", label="Once", group="tools"))
    with pytest.raises(ValueError, match="already registered"):
        register(Capability(permission="zz_once", label="Other", group="tools", sensitive=True))


def test_delegate_cannot_grant_admin_only_or_included_capabilities(api, register):
    register(Capability(permission="zz_admin_only", label="Admin only", group="tools",
                        sensitive=True, delegate_grantable=False))
    register(Capability(permission="zz_included", label="Email me", group="tools",
                        default="included", requires=("HZ_PROBE_KEY",),
                        missing="Email not configured"))
    api.gs.grant(DELEGATE, "finance", "zz_admin_only", actor="test")
    assert api.svc.ceiling(actor(DELEGATE), "finance") == {"use_hub", "ledger"}
    assert "zz_admin_only" not in api.svc.grantable(actor(DELEGATE))["finance"]

    r = _apply(api.as_(DELEGATE), "ann@x.org", ("grant", "zz_admin_only"))
    assert (r.status_code, "Outside your access" in r.json()["detail"]) == (403, True)
    assert not api.gs.can("ann@x.org", "finance", "zz_admin_only")
    assert _apply(api.as_(ROOT), "ann@x.org", ("grant", "zz_admin_only")).status_code == 200

    included = _by_id(api.svc.catalog("finance"))["zz_included"]
    assert (included["default"], included["available"], included["status"]) == (
        "included", False, "Email not configured")
    with pytest.raises(Denied) as e:
        api.svc.apply_access_change(actor(ROOT), "bob@x.org", "finance", [("grant", "zz_included")])
    assert (e.value.status, e.value.code) == (422, "included")
    assert "zz_included" not in api.svc.ceiling(actor(ROOT), "finance")


def test_obsolete_grants_stay_visible_and_removable_but_never_grantable(api):
    api.gs.grant("ann@x.org", "finance", "ledger", actor="test")
    api.gs.grant("ann@x.org", "finance", "old_tool", actor="test")
    client = api.as_(ROOT)
    body = client.get("/portal/api/access", params={"hub": "finance"}).json()
    old = _by_id(body["permissions"])["old_tool"]
    assert (old["obsolete"], old["available"], old["group"], old["status"]) == (
        True, False, "obsolete", "No longer available")
    assert "old_tool" in next(r for r in body["rows"] if r["subject"] == "ann@x.org")["perms"]

    assert _apply(client, "bob@x.org", ("grant", "old_tool")).status_code == 422
    # A delegate who does not hold it cannot remove it.
    assert _apply(api.as_(DELEGATE), "ann@x.org", ("revoke", "old_tool")).status_code == 403
    assert _apply(api.as_(ROOT), "ann@x.org", ("revoke", "old_tool")).status_code == 200
    assert api.gs.permissions_for("ann@x.org", "finance") == {"use_hub", "ledger"}
    assert "old_tool" not in _by_id(api.svc.catalog("finance"))


def test_connector_apps_appear_as_sensitive_hubzoid_tools(tmp_path, monkeypatch):
    """An Open WebUI OAuth MCP server offers connector_<app>; the catalogue
    shows it in Hubzoid tools, sensitive, without a bespoke UI row."""
    from tests import connect_helpers as h

    hub = tmp_path / "sales"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: sales\n---\nbody")
    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[], secret="cap-secret", servers=[
        {"id": "gmail", "name": "Gmail", "url": "https://gmail-mcp.example.org/mcp"}])
    h.owui_env(monkeypatch, db, "cap-secret")  # OWUI_NATIVE_MCP on
    rows = {r["permission"]: r for r in capabilities.catalog(hub)}
    gmail = rows["connector_gmail"]
    assert gmail["group"] == "tools" and gmail["sensitive"] is True
    assert "use_hub" in rows and rows["use_hub"]["group"] == "hub"
