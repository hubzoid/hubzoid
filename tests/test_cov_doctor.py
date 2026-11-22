"""`hubzoid doctor` checks (hubzoid.doctor): the AgentCore runner settings and
scheduled tasks too long for its stream, how old the last backup is (or why
it failed), and which Claude tiers a Bedrock hub really uses. No AWS calls:
`bedrock.check` is replaced where it would ask AWS."""
from __future__ import annotations

import time

import pytest

from hubzoid import doctor
from hubzoid.runner.protocol import MAX_STREAM_SECONDS


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("HUBZOID_RUNNER", "HUBZOID_AGENTCORE_RUNTIME_ARN", "HUBZOID_AGENTCORE_ENDPOINT", "HUBZOID_BACKUPS",
                "MODEL", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    h = tmp_path / "desk"
    (h / "schedule").mkdir(parents=True)
    (h / "AGENTS.md").write_text("---\nname: desk\nmodel: claude-local\n---\nx")
    return h


def test_runner_checks(hub, monkeypatch):
    assert doctor._runner_checks(hub) == []                          # turns run here
    monkeypatch.setenv("HUBZOID_RUNNER", "lambda")
    [c] = doctor._runner_checks(hub)
    assert c.status == "fail" and "use local or agentcore" in c.summary
    monkeypatch.setenv("HUBZOID_RUNNER", "agentcore")
    [c] = doctor._runner_checks(hub)
    assert c.status == "fail" and "HUBZOID_AGENTCORE_RUNTIME_ARN" in c.summary
    monkeypatch.setenv("HUBZOID_AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:eu-west-2:1:runtime/x")
    long = MAX_STREAM_SECONDS + 600
    (hub / "schedule" / "digest.md").write_text(f'---\nschedule: "0 3 * * *"\ntimeout: {long}\n---\n\nSummarise.\n')
    checks = {c.id: c for c in doctor._runner_checks(hub)}
    assert checks["runner.config"].status == "ok"
    assert checks["runner.timeouts"].status == "fail" and checks["runner.timeouts"].detail == ["digest"]


def test_backup_age(monkeypatch):
    monkeypatch.delenv("HUBZOID_BACKUPS", raising=False)   # tests/conftest.py turns them off by default
    now = time.time()
    c = doctor._backup_age(None, None)
    assert c.status == "warn" and "No backup recorded. Scheduled backups run weekly." in c.summary
    c = doctor._backup_age({"at": now - 3600, "verified": True}, None)
    assert c.status == "ok" and "Last backup 0.0 days ago." in c.summary
    c = doctor._backup_age({"at": now - 9 * 86400, "verified": False}, None)
    assert c.status == "warn" and "not verified" in c.summary
    c = doctor._backup_age({"at": now - 86400}, {"at": now - 60, "error": "disk full"})
    assert c.status == "warn" and "The last backup failed" in c.summary and "disk full" in c.summary
    monkeypatch.setenv("HUBZOID_BACKUPS", "off")
    assert "Scheduled backups are off" in doctor._backup_age(None, None).summary
    monkeypatch.setenv("HUBZOID_BACKUPS", "hourly")
    c = doctor._backup_age(None, None)
    assert c.status == "fail" and "not weekly, daily or off" in c.summary


def test_bedrock_tiers_come_from_what_the_hub_uses(hub, monkeypatch):
    station = hub / "agents" / "analyst"
    station.mkdir(parents=True)
    (station / "AGENTS.md").write_text("---\nname: analyst\ndescription: d\nisolated: true\nengine: bedrock\n"
                                       "model: haiku\ntools: []\n---\nAnalyse.\n")
    other = hub / "agents" / "coder"
    other.mkdir(parents=True)
    (other / "AGENTS.md").write_text("---\nname: coder\ndescription: d\nisolated: true\nengine: codex-local\n"
                                     "model: opus\ntools: []\n---\nCode.\n")
    (hub / "schedule" / "weekly.md").write_text('---\nschedule: "0 3 * * 1"\nmodel: bedrock/sonnet\n---\n\nReport.\n')
    assert doctor.bedrock_used_tiers(hub) == {"haiku", "sonnet"}     # the codex station's opus is not Bedrock

    from hubzoid import bedrock

    monkeypatch.setattr(bedrock, "check", lambda model, used_tiers=None: [
        ("ok", "region eu-west-2"), ("warn", f"tiers {sorted(used_tiers)} map to profiles")])
    c = doctor._bedrock_model(hub, "bedrock/sonnet")
    assert c.id == "model.credentials" and c.status == "warn"
    assert c.summary == "bedrock/sonnet: tiers ['haiku', 'sonnet'] map to profiles"
    assert c.detail[0] == "region eu-west-2"
