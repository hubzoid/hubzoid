"""Deployment CLI artifacts, overwrite refusals and offline verification output."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from hubzoid.cli import app
from hubzoid.deploy import verify
from tests.test_deploy import _hub


def test_deploy_cli_writes_files_and_reports_existing_file_conflict(tmp_path):
    hub = _hub(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["deploy", "init", "maritime", str(hub), "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["provider"] == "maritime"
    assert {f["action"] for f in body["files"]} == {"created"}
    assert "HUBZOID_AUTH=true" in (hub / "deploy" / "maritime" / "env.example").read_text()
    (hub / "Dockerfile").write_text("FROM custom\n")
    refused = runner.invoke(app, ["deploy", "init", "maritime", str(hub)])
    assert refused.exit_code == 2 and "Dockerfile" in refused.output
    assert (hub / "Dockerfile").read_text() == "FROM custom\n"
    replaced = runner.invoke(app, ["deploy", "init", "maritime", str(hub), "--force"])
    assert replaced.exit_code == 0 and "replaced" in replaced.output
    assert "Next: read deploy/maritime/README.md" in replaced.output


@pytest.mark.parametrize("as_json", [False, True])
def test_verify_cli_failure_includes_fixes_and_exit_status(monkeypatch, as_json):
    monkeypatch.setattr(verify, "run", lambda *a, **kw: [
        verify.Result("https", "ok", "HTTPS ready"),
        verify.Result("public_url", "warn", "Check the address", "Set public URL"),
        verify.Result("data", "fail", "Data public", "Turn sign-in on")])
    result = CliRunner().invoke(app, ["deploy", "verify", "https://fake.example", *(["--json"] if as_json else [])])
    assert result.exit_code == 1
    if as_json:
        body = json.loads(result.stdout)
        assert body["ok"] is False and body["checks"][2]["fix"] == "Turn sign-in on"
    else:
        assert "Not ready." in result.output and "Turn sign-in on" in result.output
        assert "Set public URL" in result.output


def test_verify_cli_invalid_address_and_success(monkeypatch):
    monkeypatch.setattr(verify, "run", Mock(side_effect=ValueError("give an origin only")))
    result = CliRunner().invoke(app, ["deploy", "verify", "bad"])
    assert result.exit_code == 2 and "give an origin only" in result.output
    monkeypatch.setattr(verify, "run", lambda *a, **kw: [verify.Result("https", "ok", "HTTPS ready")])
    result = CliRunner().invoke(app, ["deploy", "verify", "https://fake.example"])
    assert result.exit_code == 0 and "Ready." in result.output
