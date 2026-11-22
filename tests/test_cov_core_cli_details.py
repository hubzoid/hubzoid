"""CLI legacy adapter refusals, policy validation and detailed eval explanations."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import typer
from typer.testing import CliRunner

from hubzoid import cli
from hubzoid.evals import report
from hubzoid.evals.assertions import Check
from hubzoid.evals.results import CaseResult, JudgeResult, SuiteResult, ToolCallRecord, TurnRecord
from tests.test_cli_run_webapp import _hub, _isolated  # noqa: F401
from tests.test_evals_cli import no_langfuse  # noqa: F401


@pytest.mark.parametrize("command", [["slack", "run"], ["slack", "manifest"], ["inbound", "run"]])
def test_legacy_command_missing_hub_is_explicit(tmp_path, command):
    result = CliRunner().invoke(cli.app, [*command, str(tmp_path)])
    assert result.exit_code == 2 and "No AGENTS.md" in result.output


def test_slack_manifest_format_refusal_and_yaml_output(tmp_path):
    hub = _hub(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["slack", "manifest", str(hub), "--format", "xml"])
    assert result.exit_code == 2 and "--format must be json or yaml" in result.output
    result = runner.invoke(cli.app, ["slack", "manifest", str(hub), "--format", "yaml"])
    assert result.exit_code == 0 and "display_information:" in result.output and "Ops Desk" in result.output


@pytest.mark.parametrize("surface", ["slack", "inbound"])
def test_legacy_systemd_conflict_is_readable(tmp_path, monkeypatch, surface):
    from hubzoid.inbound import service as inbound
    from hubzoid.slack import service as slack

    module = slack if surface == "slack" else inbound
    monkeypatch.setattr(module, "systemd_unit_for_hub", Mock(side_effect=ValueError("deployment store differs")))
    result = CliRunner().invoke(cli.app, [surface, "systemd", str(tmp_path)])
    assert result.exit_code == 2 and "deployment store differs" in result.output


def test_legacy_slack_run_reports_env_failure_without_starting_adapter(tmp_path, monkeypatch):
    from hubzoid import supervisor
    from hubzoid.slack import adapter, env

    hub = _hub(tmp_path)
    monkeypatch.setattr(cli, "_load_settings", lambda path: None)
    monkeypatch.setattr(supervisor, "watch_parent", lambda: None)
    monkeypatch.setattr(adapter, "run", Mock(side_effect=env.EnvError("Slack tokens missing")))
    result = CliRunner().invoke(cli.app, ["slack", "run", str(hub)])
    assert result.exit_code == 2 and "Slack tokens missing" in result.output


def test_legacy_inbound_run_propagates_return_code(tmp_path, monkeypatch):
    from hubzoid import supervisor
    from hubzoid.inbound import run

    hub = _hub(tmp_path)
    monkeypatch.setattr(cli, "_load_settings", lambda path: None)
    monkeypatch.setattr(supervisor, "watch_parent", lambda: None)
    monkeypatch.setattr(run, "run", lambda path: 7)
    result = CliRunner().invoke(cli.app, ["inbound", "run", str(hub)])
    assert result.exit_code == 7


def test_deployment_sections_report_each_invalid_policy(tmp_path, capsys):
    deployment = SimpleNamespace(path=tmp_path / "hubzoid.yaml", chat={"unknown": True},
                                 brand={"unknown": True}, diagnostics={"unknown": True})
    with pytest.raises(typer.Exit) as err:
        cli._check_deployment_sections(deployment)
    output = capsys.readouterr().out
    assert err.value.exit_code == 2
    assert "chat:" in output and "brand:" in output and "diagnostics:" in output


@pytest.mark.parametrize("response", [httpx.Response(200, text="bad"), httpx.Response(503), httpx.Response(200, json=[])])
def test_health_reader_handles_invalid_body_status_and_type(monkeypatch, response):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: response)
    assert cli._health_of("http://fake") is None if response.text == "bad" else cli._health_of("http://fake") == {}


def test_health_reader_transport_error_is_unavailable(monkeypatch):
    monkeypatch.setattr(httpx, "get", Mock(side_effect=httpx.ConnectError("offline")))
    assert cli._health_of("http://fake") is None


@pytest.mark.parametrize("multi_turn", [False, True])
def test_eval_explain_includes_tools_checks_errors_and_judge(tmp_path, monkeypatch, multi_turn):
    from tests.test_evals_cli import _hub as eval_hub

    hub = eval_hub(tmp_path, {"case.md": "original prompt"})
    result = CaseResult(name="case", response="full answer", run_as="person@example.org", duration=1.25,
        tools=[ToolCallRecord(name="lookup", args={"query": "fixture"}, ok=False, error="denied", preview="returned data")],
        checks=[Check("contains", False, "missing expected")], error="run failed",
        judge=JudgeResult(score=4, threshold=7, model="fake-judge", reasoning="needs evidence"),
        turns=[TurnRecord(prompt="turn prompt", response="turn reply")] if multi_turn else None)
    saved = SuiteResult(hub="hub", cases=[result])
    monkeypatch.setattr(report, "latest", lambda path: saved)
    output = CliRunner().invoke(cli.app, ["eval", "explain", str(hub), "case"])
    assert output.exit_code == 0, output.output
    for text in ("FAIL", "person@example.org", "lookup", "returned data", "missing expected", "run failed", "needs evidence", "4/10"):
        assert text in output.output
    assert ("turn reply" if multi_turn else "full answer") in output.output
    result.judge.error = "judge unavailable"
    result.tools = []
    result.tool_calls = ["legacy_lookup"]
    output = CliRunner().invoke(cli.app, ["eval", "explain", str(hub), "case"])
    assert output.exit_code == 0 and "judge error: judge unavailable" in output.output and "legacy_lookup" in output.output
    output = CliRunner().invoke(cli.app, ["eval", "explain", str(hub), "case", "--json"])
    assert output.exit_code == 0 and json.loads(output.stdout)["name"] == "case"
