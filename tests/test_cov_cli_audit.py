"""`hubzoid audit`: the access log for people and as JSON, filtered by person
and decision, and `--export` of stored audit records as JSON lines (to a file
or the output), with the refusals for an unknown kind or a bad time."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from hubzoid import audit_log
from hubzoid.access import audit as access_audit
from hubzoid.cli import app


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_AUDIT_ARGS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_AUDIT", "db")
    h = tmp_path / "desk"
    h.mkdir()
    (h / "AGENTS.md").write_text("---\nname: desk\n---\nx")
    return h


def run(*args):
    r = CliRunner().invoke(app, ["audit", *args], env={"COLUMNS": "200"}, catch_exceptions=False)
    return r.exit_code, r.output


def test_the_access_log_for_people_and_as_json(hub):
    code, text = run(str(hub))
    assert code == 0 and "no access decisions logged yet" in text
    access_audit.record(hub, user="ana@example.org", surface="web", tool="send_invoice", decision="allow",
                        reason="granted")
    access_audit.record(hub, user="ben@example.org", surface="slack", tool="send_invoice", decision="deny",
                        reason="no_grant")
    code, text = run(str(hub))
    assert code == 0 and "ALLOW" in text and "DENY" in text and "2 entries shown" in text
    code, out = run(str(hub), "--denied", "--json")
    entries = json.loads(out)["entries"]
    assert [(e["user"], e["decision"], e["reason"]) for e in entries] == [("ben@example.org", "deny", "no_grant")]
    code, out = run(str(hub), "--user", "ana@example.org", "--json")
    assert [e["user"] for e in json.loads(out)["entries"]] == ["ana@example.org"]
    code, text = run(str(hub), "--user", "ana@example.org")
    assert "1 entry shown" in text


def test_export_writes_json_lines(hub, tmp_path):
    # Tool calls and sign-ins are stored (by a background writer); flush before reading.
    audit_log.emit("auth", "signin.ok", hub_dir=hub, actor="ana@example.org")
    audit_log.emit("tool", "tool.call", hub_dir=hub, tool="send_invoice", decision="allow")
    audit_log.flush()
    target = tmp_path / "audit.jsonl"
    code, text = run(str(hub), "--export", "--kind", "tool,auth", "--out", str(target))
    assert code == 0 and "2 record(s)" in text
    lines = [json.loads(line) for line in target.read_text().splitlines()]
    assert sorted(line["kind"] for line in lines) == ["auth", "tool"]
    code, out = run(str(hub), "--export", "--kind", "auth")
    exported = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert [r["kind"] for r in exported] == ["auth"]

    code, text = run(str(hub), "--export", "--kind", "telepathy")
    assert code == 2 and "--kind takes some of" in text
    code, text = run(str(hub), "--export", "--since", "not-a-time")
    assert code == 2
