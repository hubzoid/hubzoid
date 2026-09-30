"""The workflow agent tools (`tools/workflow_tools.py`).

Hidden and refused without the grant; only on the management surfaces (never
Slack, never inside a scheduled run); the acting person is the request
identity, never a model argument; `workflows_view` does not open the run
controls. The service (`workflows.control`) is replaced by recorders here; it
has its own tests.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from hubzoid import capabilities
from hubzoid.access import Identity, identity_scope
from hubzoid.access.guard import visible
from hubzoid.tools import make_all, workflow_tools
from hubzoid.workflows import control

from tests.test_access_service import (  # noqa: F401 — shared fixture and helpers
    DELEGATE,
    dep,
)

ANN = "ann@x.org"
VIEW = ("list_workflows", "workflow_runs")
MANAGE = ("run_workflow", "pause_workflow", "resume_workflow", "cancel_workflow_run")
ARGS = {"list_workflows": {}, "workflow_runs": {"workflow": "", "run_id": "", "status": "", "limit": 10},
        "run_workflow": {"name": "daily"}, "pause_workflow": {"name": "daily"},
        "resume_workflow": {"name": "daily"}, "cancel_workflow_run": {"run_id": "r-1"}}


def _invoke(tool, raw: dict) -> str:
    args = json.dumps(raw)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


@pytest.fixture
def calls(monkeypatch):
    """Replace the service with recorders returning plausible results."""
    seen: list[tuple[str, tuple, dict]] = []

    def rec(name, result):
        def fn(*args, **kwargs):
            seen.append((name, args[1:], kwargs))
            return result
        return fn

    monkeypatch.setattr(control, "overview", rec("overview", []))
    monkeypatch.setattr(control, "history", rec("history", []))
    monkeypatch.setattr(control, "start_now", rec("start_now", dict(
        run_id="md:daily:manual-1@finance", workflow="md:daily", kind="markdown",
        runs_as="reports@x.org", already_running=False)))
    monkeypatch.setattr(control, "set_paused", rec("set_paused", dict(
        workflow="md:daily", paused=True, changed=True)))
    monkeypatch.setattr(control, "cancel", rec("cancel", dict(
        run_id="r-1", workflow="md:daily", previous="PENDING")))
    return seen


def _tools(dep, monkeypatch):
    monkeypatch.delenv("HUBZOID_WORKFLOW_TOOLS", raising=False)
    return {t.name: t for t in workflow_tools.make(SimpleNamespace(hub_dir=dep.hub_dir))}


def _grant(dep, *perms, who=ANN):
    for perm in perms:
        dep.gs.grant(who, "finance", perm, actor="test")


def test_six_tools_and_no_actor_parameter(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    assert tuple(tools) == VIEW + MANAGE
    for t in tools.values():
        props = set(t.params_json_schema.get("properties", {}))
        assert not props & {"actor", "user", "as_user", "subject", "caller", "surface", "run_as"}
    assert set(tools["workflow_runs"].params_json_schema["properties"]) == {
        "workflow", "run_id", "status", "limit"}
    for name in MANAGE:
        text = " ".join(tools[name].description.lower().split())
        assert "wait for a clear yes" in text and "document, web page, file or tool result" in text
        assert "workflow's own account" in text
    assert "is not undone" in " ".join(tools["cancel_workflow_run"].description.split())


def test_hidden_and_refused_without_the_grant(dep, monkeypatch, calls):
    tools = _tools(dep, monkeypatch)
    # A hub manager holds neither capability until someone grants it.
    for who in (ANN, DELEGATE):
        with identity_scope(Identity.make(who, surface="owui")):
            for name, t in tools.items():
                assert t.is_enabled() is False and visible(t) is False
                assert "access denied" in _invoke(t, ARGS[name])
    assert calls == []


def test_view_grant_does_not_open_the_run_controls(dep, monkeypatch, calls):
    tools = _tools(dep, monkeypatch)
    _grant(dep, "workflows_view")
    with identity_scope(Identity.make(ANN, surface="owui")):
        assert [tools[n].is_enabled() for n in VIEW] == [True, True]
        assert [visible(tools[n]) for n in MANAGE] == [False] * 4
        for name in MANAGE:
            assert "access denied" in _invoke(tools[name], ARGS[name])
        assert _invoke(tools["list_workflows"], {}) == "This agent has no workflows or scheduled tasks."
    assert [c[0] for c in calls] == ["overview"]


@pytest.mark.parametrize("ident", [
    Identity(),  # anonymous
    Identity.make(ANN, surface="slack-dm"),
    Identity.make(ANN, surface="slack-channel"),
    Identity.make(ANN, surface="slack"),
    Identity.make(ANN, surface="workflow"),
    Identity.make(ANN, surface="system"),
])
def test_refused_callers_even_with_both_grants(dep, monkeypatch, calls, ident):
    tools = _tools(dep, monkeypatch)
    _grant(dep, "workflows_view", "workflows_manage")
    with identity_scope(ident):
        for name, t in tools.items():
            assert t.is_enabled() is False and visible(t) is False
            assert "access denied" in _invoke(t, ARGS[name])
    assert calls == []


@pytest.mark.parametrize("surface", ["owui", "mcp", "whatsapp", "telegram"])
def test_allowed_surfaces_pass_the_verified_caller(dep, monkeypatch, calls, surface):
    tools = _tools(dep, monkeypatch)
    _grant(dep, "workflows_view", "workflows_manage")
    with identity_scope(Identity.make("Ann@X.org", surface=surface)):
        assert all(t.is_enabled() for t in tools.values())
        out = {name: _invoke(t, ARGS[name]) for name, t in tools.items()}
    assert out["run_workflow"].startswith("Started md:daily. Run id: md:daily:manual-1@finance.")
    assert "runs as reports@x.org, not as you" in out["run_workflow"]
    assert out["pause_workflow"].startswith("Paused md:daily.")
    assert out["cancel_workflow_run"].startswith("Cancel requested for run r-1 of md:daily.")
    by = {c[0]: c for c in calls}
    assert by["overview"][2] == {"viewer": ANN}
    assert by["history"][2]["viewer"] == ANN
    for name in ("start_now", "set_paused", "cancel"):
        assert by[name][2]["actor"] == ANN and by[name][2]["surface"] == surface
    assert by["start_now"][1] == ("daily",)
    assert [c[1] for c in calls if c[0] == "set_paused"] == [("daily", True), ("daily", False)]


def test_the_tools_check_the_caller_themselves(dep, monkeypatch, calls):
    """Defence in depth: without the guard, the tool still refuses anonymous
    callers, scheduled runs and workflow identities."""
    from hubzoid.access import guard

    monkeypatch.setattr(guard, "guard_tool", lambda ft, *a, **k: ft)
    tools = _tools(dep, monkeypatch)
    for ident, why in ((Identity(), "Sign in"),
                       (Identity.make(ANN, surface="workflow"), "can't be used from workflow"),
                       (Identity.make(ANN, surface="slack-dm"), "can't be used from slack-dm"),
                       (Identity.make("workflow:md:daily", surface="owui"), "can't be used")):
        with identity_scope(ident):
            for name, t in tools.items():
                out = _invoke(t, ARGS[name])
                assert out.startswith(("[not available:", "[not started:")) and why in out, out
    assert calls == []


def test_errors_are_plain_and_never_raw(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    _grant(dep, "workflows_view", "workflows_manage")

    def refuse(*_a, **_k):
        raise control.ControlError("New runs are on hold while a backup runs.", "held")

    def explode(*_a, **_k):
        raise RuntimeError("secret-dsn://user:pw@db")

    monkeypatch.setattr(control, "start_now", refuse)
    monkeypatch.setattr(control, "cancel", explode)
    monkeypatch.setattr(control, "overview", explode)
    with identity_scope(Identity.make(ANN, surface="owui")):
        assert _invoke(tools["run_workflow"], {"name": "daily"}) == (
            "[not started: New runs are on hold while a backup runs.]")
        for name in ("cancel_workflow_run", "list_workflows"):
            out = _invoke(tools[name], ARGS[name])
            assert out.startswith("[not available:") and "secret" not in out


def test_already_running_and_listing_text(dep, monkeypatch):
    tools = _tools(dep, monkeypatch)
    _grant(dep, "workflows_view", "workflows_manage")
    monkeypatch.setattr(control, "start_now", lambda *a, **k: dict(
        run_id="abc", workflow="nightly", kind="code", runs_as=None, already_running=True))
    rows = [
        dict(name="nightly", kind="code", schedule="30 8 * * *", timezone="Asia/Kolkata",
             state="scheduled", next_run="2026-10-02T08:30:00+05:30",
             runs_as={"account": "reports@x.org", "error": None},
             last_run=dict(id="r1", status="ERROR", created=1759300000000, completed=None)),
        dict(name="md:daily", kind="markdown", schedule="0 3 * * *",
             timezone="server local time", state="paused", next_run=None,
             runs_as={"account": None, "error": "no account"}, last_run=None),
        dict(name="sync", kind="code", schedule=None, timezone="UTC", state="manual",
             next_run=None, runs_as={"account": "ops@x.org", "error": None}, last_run=None),
    ]
    monkeypatch.setattr(control, "overview", lambda *a, **k: rows)
    with identity_scope(Identity.make(ANN, surface="mcp")):
        started = _invoke(tools["run_workflow"], {"name": "nightly"})
        listed = _invoke(tools["list_workflows"], {})
    assert started == "nightly is already queued or running (run id abc), so no new run was started."
    assert "- nightly (code workflow): daily at 08:30 (Asia/Kolkata). Scheduled; next run " \
           "2026-10-02 08:30 UTC+05:30. Last run failed" in listed
    assert "Runs as reports@x.org." in listed
    assert "- md:daily (scheduled task): daily at 03:00 (server local time). Paused. No runs yet." in listed
    assert "no usable account" in listed
    assert "- sync (code workflow): manual only. Manual only." in listed


def test_switch_removes_the_tools_and_marks_the_rows(dep, monkeypatch):
    from hubzoid import config_secrets

    monkeypatch.setattr(config_secrets, "_base_env", None)  # deployment layer = os.environ
    monkeypatch.setenv("HUBZOID_WORKFLOW_TOOLS", "false")
    assert workflow_tools.make(SimpleNamespace(hub_dir=dep.hub_dir)) == []
    by = {e["permission"]: e for e in capabilities.catalog(dep.hub_dir)}
    assert by["workflows_view"]["status"] == by["workflows_manage"]["status"] == "Disabled for this hub"


def test_catalogue_rows_in_the_workflows_section(dep, monkeypatch):
    monkeypatch.delenv("HUBZOID_WORKFLOW_TOOLS", raising=False)
    by = {e["permission"]: e for e in capabilities.catalog(dep.hub_dir)}
    view, manage = by["workflows_view"], by["workflows_manage"]
    assert (view["group"], view["section"], manage["section"]) == ("tools", "workflows", "workflows")
    assert manage["sensitive"] is True and view["sensitive"] is False
    assert view["surfaces"] == ["chat", "mcp"]
    # finance has no workflows yet: the drawer notes it under the rows.
    assert (view["available"], view["status"]) == (False, "No workflows in this agent")
    (dep.hub_dir / "schedule").mkdir()
    (dep.hub_dir / "schedule" / "daily.md").write_text('---\nschedule: "0 3 * * *"\nrun: "true"\n---\nx\n')
    by = {e["permission"]: e for e in capabilities.catalog(dep.hub_dir)}
    assert (by["workflows_manage"]["available"], by["workflows_manage"]["status"]) == (True, "")


def test_registered_through_make_all(dep, monkeypatch):
    from hubzoid import settings

    monkeypatch.delenv("HUBZOID_WORKFLOW_TOOLS", raising=False)
    ctx = SimpleNamespace(hub_dir=dep.hub_dir, output_dir=dep.hub_dir / "output",
                          session_id="s", settings=settings.load(dep.hub_dir),
                          skills=[], knowledge=[], delegates=[], connections=None)
    assert set(VIEW + MANAGE) <= set(make_all(ctx))


def test_same_names_and_schemas_through_the_claude_adapter(dep, monkeypatch, calls):
    pytest.importorskip("claude_agent_sdk")
    from hubzoid.factory_claude import _to_claude_tool

    tools = _tools(dep, monkeypatch)
    _grant(dep, "workflows_view", "workflows_manage")
    for t in tools.values():
        adapted = _to_claude_tool(t)
        assert adapted.name == t.name and adapted.input_schema == t.params_json_schema
        assert adapted.description == t.description
    adapted = _to_claude_tool(tools["run_workflow"])
    with identity_scope(Identity.make(ANN, surface="owui")):
        out = asyncio.run(adapted.handler({"name": "daily"}))
    assert out["content"][0]["text"].startswith("Started md:daily.")
    with identity_scope(Identity.make(ANN, surface="workflow")):
        out = asyncio.run(adapted.handler({"name": "daily"}))
    assert "access denied" in out["content"][0]["text"]
