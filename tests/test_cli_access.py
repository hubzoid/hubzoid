"""CLI tests for the access commands (grant / revoke / access check|list|bootstrap)."""
from __future__ import annotations

from typer.testing import CliRunner

from hubzoid.cli import app

runner = CliRunner()


def test_grant_check_revoke_roundtrip(tmp_path):
    hub = str(tmp_path)

    r = runner.invoke(app, ["grant", "alice", "prod_in", "--hub", "finance", hub])
    assert r.exit_code == 0, r.output
    assert "granted" in r.output

    r = runner.invoke(app, ["access", "check", "alice", "--hub", "finance", hub])
    assert r.exit_code == 0, r.output
    assert "prod_in" in r.output and "use_hub" in r.output  # implication

    r = runner.invoke(app, ["access", "list", "--hub", "finance", hub])
    assert r.exit_code == 0 and "alice" in r.output

    r = runner.invoke(app, ["revoke", "alice", "prod_in", "--hub", "finance", hub])
    assert r.exit_code == 0 and "revoked" in r.output

    r = runner.invoke(app, ["access", "check", "alice", "--hub", "finance", hub])
    assert "use_hub" in r.output and "prod_in" not in r.output  # tool gone, entry kept


def test_bootstrap_and_last_admin_guard(tmp_path):
    hub = str(tmp_path)
    r = runner.invoke(app, ["access", "bootstrap", "--admin", "root", "--authoritative", hub])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["access", "check", "root", "--hub", "anything", hub])
    assert "manage_access" in r.output  # org admin spans all hubs

    # last-admin guard: refuse and exit non-zero
    r = runner.invoke(app, ["revoke", "root", "manage_access", "--org", hub])
    assert r.exit_code == 1
    assert "refused" in r.output.lower()


def test_new_workflow_scaffold(tmp_path):
    r = runner.invoke(app, ["new", "workflow", "review-prs", str(tmp_path)])
    assert r.exit_code == 0, r.output
    main = tmp_path / "workflows" / "review-prs" / "main.py"
    assert main.exists()
    body = main.read_text()
    assert "@workflow(" in body and "def review_prs()" in body
    # scaffolding twice refuses
    r = runner.invoke(app, ["new", "workflow", "review-prs", str(tmp_path)])
    assert r.exit_code == 1
