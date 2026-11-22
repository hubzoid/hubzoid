"""`hubzoid workflow-access` (hubzoid.workflows.access_cli): resources with
what you may do, export to a file, validate, diff and apply a policy (only
for someone who manages access, compare-and-set on the revision), explain why
someone may or may not act (asking about others needs manage_access), human
and JSON output, and a folder that isn't a hub. Uses the hub of
tests/test_workflow_access.py."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from hubzoid.workflows.access_cli import app
from tests.test_workflow_access import POLICY, hub  # noqa: F401

OWNER = "owner@example.org"


def run(*args, json_out=True):
    r = CliRunner().invoke(app, [*args, *(["--json"] if json_out else [])], catch_exceptions=False)
    return r.exit_code, (json.loads(r.output) if json_out else r.output)


def test_resources_export_validate_diff_and_apply(hub, tmp_path):  # noqa: F811
    code, out = run("resources", str(hub), "--user", "ana@example.org")
    items = {r["key"]: r for r in out["items"]}
    assert code == 0 and "weekly_report" in items and items["weekly_report"]["allowed_actions"] == []
    code, text = run("resources", str(hub), "--user", "ana@example.org", json_out=False)
    assert "weekly_report" in text and "you: nothing" in text

    code, exported = run("export", str(hub))
    assert code == 0 and exported["revision"] == 0 and "grants: []" in exported["yaml"]
    code, text = run("export", str(hub), "--output", "workflows/access.yaml", json_out=False)
    assert code == 0 and "Wrote workflows/access.yaml (revision 0)." in text

    policy = tmp_path / "access.yaml"
    policy.write_text(POLICY)
    code, out = run("validate", str(hub), "--file", str(policy))
    assert code == 0 and out == {"valid": True, "grants": 5}
    code, text = run("diff", str(hub), "--file", str(policy), json_out=False)
    assert "+ ana@example.org weekly_report run" in text
    assert all(line.startswith("+ ") for line in text.strip().splitlines())        # nothing removed

    code, err = run("apply", str(hub), "--file", str(policy), "--expected-revision", "0", "--user", "ana@example.org")
    assert code != 0 and err["detail"]["code"] == "delegation_denied"
    code, text = run("apply", str(hub), "--file", str(policy), "--expected-revision", "0", "--user", OWNER,
                     json_out=False)
    assert "Applied revision 1: 5 added, 0 removed." in text
    code, err = run("apply", str(hub), "--file", str(policy), "--expected-revision", "0", "--user", OWNER)
    assert code != 0 and err["detail"]["code"] == "revision_conflict"
    code, text = run("diff", str(hub), "--file", str(policy), json_out=False)
    assert text.strip() == "No change."
    code, text = run("export", str(hub), json_out=False)
    assert text.startswith("# revision 1\n") and "subject: ana@example.org" in text


def test_explain_and_refusals(hub, tmp_path):  # noqa: F811
    policy = tmp_path / "access.yaml"
    policy.write_text(POLICY)
    run("apply", str(hub), "--file", str(policy), "--expected-revision", "0", "--user", OWNER)
    code, out = run("explain", str(hub), "--workflow", "weekly_report", "--action", "run", "--user", "ana@example.org")
    assert code == 0 and out["allowed"] is True and out["subject"] == "ana@example.org"
    code, text = run("explain", str(hub), "--subject", "ben@example.org", "--workflow", "weekly_report",
                     "--action", "run", "--user", OWNER, json_out=False)
    assert text.startswith("ben@example.org may not run weekly_report:")
    code, err = run("explain", str(hub), "--subject", "ben@example.org", "--workflow", "weekly_report",
                    "--action", "run", "--user", "ana@example.org")
    assert code != 0 and err["detail"]["code"] == "permission_denied"
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\ngrants:\n  - subject: ana@example.org\n    workflow: nope\n    actions: [run]\n")
    code, err = run("validate", str(hub), "--file", str(bad))
    assert code != 0 and err["detail"]["code"] == "unknown_workflow"
    code, err = run("resources", str(tmp_path))
    assert code != 0 and "is not a hub folder" in err["detail"]["message"]
