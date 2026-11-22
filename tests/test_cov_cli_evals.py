"""`hubzoid eval list|explain`: every kind of check a case can declare is
described, and explain refuses before any run and for an unknown case. The
run, status and explain journeys are in tests/test_evals_cli.py."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from hubzoid.cli import app
from tests.test_evals_cli import _hub


def run(*args):
    r = CliRunner().invoke(app, ["eval", *args], env={"COLUMNS": "250"}, catch_exceptions=False)
    return r.exit_code, r.output


def test_list_describes_each_kind_of_check(tmp_path):
    hub = _hub(tmp_path, {
        "multi.md": "---\nforbid_tools: [send_email]\nnot_contains: [password]\ntags: [smoke]\n"
                    "run_as: ana@example.org\n---\n## Turn 1\nfirst question\n\n## Turn 2\nfollow up\n",
        "args.md": "---\nexpect_tool_args:\n  lookup_order: {id: '7'}\ncontains: [shipped]\n---\nwhere is 7?",
    })
    code, text = run("list", str(hub))
    assert code == 0
    assert "2 turns" in text and "forbids send_email" in text and "not_contains 1" in text
    assert "tags: smoke" in text and "runs as ana@example.org" in text
    assert "checks arguments of lookup_order" in text and "contains 1" in text
    code, out = run("list", str(hub), "--json")
    assert {c["id"] for c in json.loads(out)["cases"]} == {"multi", "args"}


def test_a_broken_case_and_explain_refusals(tmp_path):
    hub = _hub(tmp_path, {"bad.md": "---\nturns: [a, b]\n---\nhi"})
    code, out = run("list", str(hub), "--json")
    detail = json.loads(out)["detail"]
    assert code == 2 and detail["code"] == "invalid_case" and "unknown frontmatter key(s): turns" in detail["message"]
    code, out = run("explain", str(hub), "anything", "--json")
    assert code == 1 and json.loads(out)["detail"]["code"] == "not_found"
