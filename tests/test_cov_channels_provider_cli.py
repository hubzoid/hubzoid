"""Provider CLI commands render concrete setup results and forward exact user choices."""
from __future__ import annotations

import json
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from hubzoid.channels import cli, config, store


@pytest.fixture
def provider(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "WEBUI_AUTH"):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "desk"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nDesk.")
    conn = SimpleNamespace(provider="email", id="main", hub="desk", revision=7, state={}, hub_dir=root,
                           settings=SimpleNamespace(mailbox="desk@example.org", workspace_id="W1"))
    monkeypatch.setattr(config, "active", lambda *a: conn)
    return root, conn


@pytest.mark.parametrize("as_json", [True, False])
@pytest.mark.parametrize("name,command,module,function,result,extra,word", [
    ("email", "status", "status", "status", {"connected": True}, [], "connected"),
    ("email", "connect", "oauth", "connect", {"authorization_url": "https://consent.test", "expires_in": 600}, [], "https://consent.test"),
    ("email", "poll", "poller", "poll_once", {"state": "started"}, [], "Started"),
    ("email", "rehearse-draft", "status", "rehearse_draft", {"draft_id": "draft1"}, [], "draft1"),
    ("email", "import-history", "history", "import_history", {"digest": "abc"},
     ["--query", "label:orders", "--after", "2026-01-01", "--before", "2026-02-01"], "abc"),
    ("linear", "setup", "oauth", "setup_checklist", {"steps": ["Enter URL"], "scopes": ["read"], "installed": False}, [], "Enter URL"),
    ("linear", "connect", "oauth", "connect", {"authorization_url": "https://consent.test"}, [], "https://consent.test"),
    ("linear", "disconnect", "oauth", "disconnect", {"disconnected": True}, [], "Disconnected"),
    ("clickup", "setup", "adapter", "setup", {"endpoint": "https://hub.test/hook", "made": ["L1"], "kept": ["L2"], "removed": ["old"]}, [], "https://hub.test/hook"),
    ("clickup", "teardown", "adapter", "teardown", {"deleted": ["hook1"]}, [], "Deleted 1"),
    ("github", "setup status", "setup", "status", {"state": "awaiting_installation", "installation_url": "https://install.test"}, [], "https://install.test"),
    ("github", "setup verify", "setup", "installed", {"installation_id": 9, "owner": "example", "revision": 8}, ["--installation-id", "9"], "verified"),
    ("github", "setup cancel", "setup", "cancel", None, [], "cancelled"),
    ("github", "import-app", "setup", "import_app", {"revision": 8}, ["--app-id", "1", "--installation-id", "9", "--owner-id", "2"], "imported"),
    ("github", "repositories", "setup", "repositories", [{"id": 1, "full_name": "example/x"}], [], "example/x"),
    ("github", "configure", "setup", "configure", {"revision": 8, "status": "enabled"},
     ["--expected-revision", "7", "--set", "enabled=true", "--set", "name=hello", "--repository", "example/x"], "enabled"),
    ("github", "intake", "setup", "manual_intake", {"delivery_id": "delivery-123"},
     ["--url", "https://github.com/example/x/issues/1", "--task", "Fix", "--route", "triage"], "delivery"),
])
def test_provider_command_results(provider, monkeypatch, as_json, name, command, module, function, result, extra, word):
    root, conn = provider
    conn.provider = name
    fn = Mock(return_value=result)
    monkeypatch.setattr(import_module(f"hubzoid.channels.{name}.{module}"), function, fn)
    out = CliRunner().invoke(cli.channels_app, [name, *command.split(), str(root), "main", *extra,
                                               *(["--json"] if as_json else [])])
    assert out.exit_code == 0, out.stdout
    if as_json:
        expected = {"state": "none"} if command == "setup cancel" else {"items": result} if command == "repositories" else result
        assert json.loads(out.stdout) == expected
    else:
        assert word in out.stdout
    assert fn.call_args.args[0] is conn
    if command == "configure":
        assert fn.call_args.kwargs["sets"] == {"enabled": True, "name": "hello"}
        assert fn.call_args.kwargs["repositories_"] == ["example/x"]
    if command == "intake":
        assert fn.call_args.kwargs["route"] == "triage"


@pytest.mark.parametrize("name,command", [("email", "status"), ("linear", "setup"), ("clickup", "members"),
                                         ("github", "repositories"), ("slack", "manifest")])
def test_provider_commands_refuse_other_channel_types(provider, name, command):
    root, conn = provider
    conn.provider = "fake"
    out = CliRunner().invoke(cli.channels_app, [name, command, str(root), "main", "--json"])
    assert out.exit_code == 1 and json.loads(out.stdout)["detail"]["code"] == "not_found"


@pytest.mark.parametrize("as_json", [True, False])
def test_clickup_members_and_linear_sessions(provider, monkeypatch, as_json):
    from hubzoid.channels.clickup import api
    root, conn = provider
    conn.provider = "clickup"
    monkeypatch.setattr(api, "call", lambda *a: {"teams": [{"id": "W1", "members": [
        {"user": {"id": 42, "username": "Ana", "email": "ana@example.org"}}]}]})
    out = CliRunner().invoke(cli.channels_app, ["clickup", "members", str(root), "main", *(["--json"] if as_json else [])])
    assert out.exit_code == 0
    assert json.loads(out.stdout) == {"items": [{"id": "42", "name": "Ana", "email": "ana@example.org"}]} if as_json else "42  Ana" in out.stdout
    conn.provider = "linear"
    monkeypatch.setattr(store, "list_threads", lambda *a: {"items": [{"id": "t1", "requester": "ana", "origin_json": {
        "coordinates": {"agent_session_id": "s1"}, "url": "https://linear.test/s1"}}]})
    out = CliRunner().invoke(cli.channels_app, ["linear", "sessions", str(root), "main", *(["--json"] if as_json else [])])
    assert out.exit_code == 0 and "s1" in out.stdout and "ana" in out.stdout


@pytest.mark.parametrize("installed", [True, False])
def test_linear_wait_uses_current_installation_state_without_sleep(provider, monkeypatch, installed):
    from hubzoid.channels.linear import cli as linear_cli, oauth
    root, conn = provider
    conn.provider = "linear"
    conn.state = {"linear_app_user_id": "app1", "linear_org_name": "Example"} if installed else {}
    monkeypatch.setattr(oauth, "connect", lambda *a, **kw: {"authorization_url": "https://consent.test"})
    ticks = iter([0, 0, 20])
    monkeypatch.setattr(linear_cli.time, "time", lambda: next(ticks))
    monkeypatch.setattr(linear_cli.time, "sleep", lambda _: None)
    out = CliRunner().invoke(cli.channels_app, ["linear", "connect", str(root), "main", "--wait", "10"])
    assert out.exit_code == 0
    assert ("Connected to Example" if installed else "Not approved yet") in out.stdout


def test_github_configure_rejects_missing_assignment(provider):
    root, conn = provider
    conn.provider = "github"
    out = CliRunner().invoke(cli.channels_app, ["github", "configure", str(root), "main", "--expected-revision", "7",
                                               "--set", "enabled", "--json"])
    assert out.exit_code == 2 and "needs KEY=VALUE" in json.loads(out.stdout)["detail"]["message"]


def test_github_call_reads_argument_file(provider, monkeypatch):
    from hubzoid.channels.github import operations
    root, conn = provider
    conn.provider = "github"
    file = root / "args.json"
    file.write_text('{"repository_id": 1}')
    fn = Mock(return_value={"data": {"id": 1}})
    monkeypatch.setattr(operations, "call", fn)
    out = CliRunner().invoke(cli.channels_app, ["github", "call", str(root), "main", "--operation", "getRepository",
                                               "--arguments-file", str(file), "--json"])
    assert out.exit_code == 0 and json.loads(out.stdout) == {"data": {"id": 1}}
    assert fn.call_args.args == (conn, "getRepository", {"repository_id": 1})


@pytest.mark.parametrize("fmt,as_json", [("json", True), ("yaml", False)])
def test_slack_manifest_writes_requested_file(provider, monkeypatch, fmt, as_json):
    from hubzoid.channels.slack import manifest
    root, conn = provider
    conn.provider = "slack"
    fn = Mock(return_value='{"display_information": {"name": "Desk"}}')
    monkeypatch.setattr(manifest, "render", fn)
    target = root / "manifest.txt"
    out = CliRunner().invoke(cli.channels_app, ["slack", "manifest", str(root), "main", "--output", str(target),
                                               "--format", fmt, *(["--json"] if as_json else [])])
    assert out.exit_code == 0 and target.read_text().endswith("\n")
    assert json.loads(target.read_text())["display_information"]["name"] == "Desk"
    assert fn.call_args.args == (conn, fmt)
    assert json.loads(out.stdout)["revision"] == 7 if as_json else "revision 7" in out.stdout
