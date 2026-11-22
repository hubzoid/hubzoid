"""Approval CLI configuration, visibility and interactive confirmation boundaries."""
from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from hubzoid.hitl import cli, config, service
from tests.test_hitl import RULES, caller, hub  # noqa: F401


def invoke(hub, app, command, *args, **kw):
    return CliRunner().invoke(app, [command, str(hub), *args], **kw)


def request(hub):
    (hub / "hitl.yaml").write_text(RULES)
    return service.check(hub, hub.name, caller(), tool="reserve_stock", arguments={"sku": 1}).request


def test_config_cli_absent_validate_export_apply_conflict(hub):
    r = invoke(hub, cli.hitl_app, "validate", "--json")
    assert json.loads(r.output)["exists"] is False
    assert "no hitl.yaml" in invoke(hub, cli.hitl_app, "export").output
    r = invoke(hub, cli.hitl_app, "export", "--json")
    assert json.loads(r.output) == {"revision": "absent", "text": None, "exists": False}
    source = hub / "draft.yaml"
    source.write_text(RULES)
    r = invoke(hub, cli.hitl_app, "apply", "--file", str(source), "--expected-revision", "absent", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["revision"] == config.revision_of(RULES)
    r = invoke(hub, cli.hitl_app, "validate", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["valid"] is True
    r = invoke(hub, cli.hitl_app, "apply", "--file", str(source), "--expected-revision", "absent", "--json")
    assert r.exit_code == 4 and json.loads(r.output)["detail"]["code"] == "revision_conflict"
    source.write_text("version: 2\n")
    for cmd in ("validate", "apply"):
        args = ["--file", str(source), "--json"]
        if cmd == "apply":
            args += ["--expected-revision", "absent"]
        r = invoke(hub, cli.hitl_app, cmd, *args)
        assert r.exit_code == 2 and json.loads(r.output)["detail"]["code"] == "invalid_config"


def test_invalid_hub_and_missing_user(hub):
    r = invoke(hub / "absent", cli.hitl_app, "validate", "--json")
    assert r.exit_code == 2 and json.loads(r.output)["detail"]["code"] == "invalid_request"
    r = invoke(hub, cli.requests_app, "list", "--user", "missing@example.org", "--json")
    assert r.exit_code == 2 and json.loads(r.output)["detail"]["code"] == "not_found"


def test_request_cli_listing_short_ids_visibility_and_cancel(hub):
    req = request(hub)
    args = ["--user", "ana@example.org", "--json"]
    r = invoke(hub, cli.requests_app, "list", *args)
    assert r.exit_code == 0 and json.loads(r.output)["items"][0]["id"] == req["id"]
    r = invoke(hub, cli.requests_app, "show", req["id"][:8], *args)
    assert json.loads(r.output)["id"] == req["id"]
    r = invoke(hub, cli.requests_app, "show", req["id"], "--user", "ben@example.org", "--json")
    assert r.exit_code == 2 and json.loads(r.output)["detail"]["code"] == "not_found"
    r = invoke(hub, cli.requests_app, "decide", req["id"], "--decision", "approve", *args)
    assert r.exit_code == 3 and json.loads(r.output)["detail"]["code"] == "human_action_required"
    assert service.get_for(hub, hub.name, req["id"], "u_ana", "ana@example.org")["status"] == "pending"
    r = invoke(hub, cli.requests_app, "cancel", req["id"], *args)
    assert r.exit_code == 0 and json.loads(r.output)["status"] == "cancelled"
    assert "Nothing waits for you" in invoke(hub, cli.requests_app, "list", "--user", "ana@example.org").output
    assert "cancelled" in invoke(hub, cli.requests_app, "list", "--all", "--user", "ana@example.org").output
    assert cli._full_id(hub, "unknown") == "unknown"


@pytest.mark.parametrize("confirm,status", [("ABCD", "approved"), ("NO", "pending")])
def test_interactive_decision_rechecks_fingerprint(hub, monkeypatch, confirm, status, capsys):
    req = request(hub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda n: "abcd")
    monkeypatch.setattr(typer, "prompt", lambda text: confirm)
    if status == "pending":
        with pytest.raises(typer.Exit) as exc:
            cli.r_decide(hub, req["id"], "ana@example.org", "approve", None, None, True)
        assert exc.value.exit_code == 2
    else:
        cli.r_decide(hub, req["id"], "ana@example.org", "approve", None, None, True)
    assert service.get_for(hub, hub.name, req["id"], "u_ana", "ana@example.org")["status"] == status


@pytest.mark.parametrize("text,path", [
    ("version: 1\nquestions: maybe", "/questions"),
    ("version: 1\napprovers: []", "/approvers"),
    ("version: 1\ntools: {x: 42}", "/tools/x"),
    ("version: 1\ntools: {x: {other: true}}", "/tools/x/other"),
    ("version: 1\ntools: {x: {allow_always: 1}}", "/tools/x/allow_always"),
    ("version: 1\nagents: {x: {questions: nope}}", "/agents/x"),
    ("version: 1\nunknown: 1", "/unknown"),
])
def test_rule_validation_names_the_key(text, path):
    with pytest.raises(config.ConfigError) as exc:
        config.parse(text)
    assert exc.value.as_dict()["path"] == path and exc.value.line is not None


def test_named_approvers_agent_questions_and_broken_file(hub):
    cfg = config.parse("version: 1\napprovers: [' ana@example.org ']\nquestions: true\n"
                       "tools: {x: always}\nagents: {intake: {questions: false}}\n")
    assert cfg.approvers == ("ana@example.org",) and cfg.rule_for("x").ask == "always"
    assert not cfg.questions_for("intake") and cfg.questions_for("other")
    assert config.broken(hub) is None
    (hub / "hitl.yaml").write_text("version: 2\n")
    assert config.broken(hub).path == "/version"
    (hub / "hitl.yaml").write_text("version: 1\n")
    assert config.broken(hub) is None
