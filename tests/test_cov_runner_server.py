"""The AgentCore runner's HTTP contract (hubzoid.runner.server) in process:
ping, warmup, refused invocations (other release, no work order), a hello
call, and what the warmup reads and never reads. The live-runner journeys are
in tests/test_agentcore_runner.py."""
from __future__ import annotations

import json
import os
import stat
import sys
import types

import pytest
from starlette.testclient import TestClient

from hubzoid.runner import calls, protocol, server
from tests.test_agentcore_runner import hub  # noqa: F401

SESSION = {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "hz-x-" + "1" * 40}


@pytest.fixture
def fake_sdk(tmp_path, monkeypatch):
    """A stand-in claude_agent_sdk package folder, so warming never runs the real CLI."""
    pkg = tmp_path / "sdk" / "claude_agent_sdk"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    module = types.ModuleType("claude_agent_sdk")
    module.__file__ = str(pkg / "__init__.py")
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return pkg


def events(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


def test_ping_and_warmup(hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(server, "_cli_first_run", lambda: {"ran": False, "why": "test"})
    read = []
    monkeypatch.setattr(server, "_warm_disk", lambda hub_dir: read.append(hub_dir))
    with TestClient(server.create_app(hub)) as client:
        assert client.get("/ping").json() == {"status": "Healthy"}
        r = client.post("/warmup", headers={"x-amzn-bedrock-agentcore-runtime-warmup-type": "Memory"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert read == [hub.resolve()]


def test_a_call_for_another_release_or_without_a_work_order_is_refused(hub):  # noqa: F811
    with TestClient(server.create_app(hub)) as client:
        other = {"v": 1, "kind": "turn", "call": "rc_old", "release": {"hubzoid": "0.0.1", "image": ""}}
        got = events(client.post("/invocations", json=other, headers=SESSION).text)
        assert got[0]["type"] == "refused" and got[0]["code"] == "version"
        assert "0.0.1" in got[0]["reason"]

        none = {"v": 1, "kind": "turn", "call": "rc_none", "release": protocol.release()}
        got = events(client.post("/invocations", json=none, headers=SESSION).text)
        assert got[0]["type"] == "refused" and "no matching work order" in got[0]["reason"]


def test_hello_answers_from_a_claimed_work_order(hub):  # noqa: F811
    body = {"v": 1, "kind": "hello", "call": "rc_hello", "release": protocol.release()}
    calls.create(hub, call_id="rc_hello", hub="h", kind="hello", session_id=SESSION[next(iter(SESSION))],
                 request_digest=protocol.digest(body), identity={})
    with TestClient(server.create_app(hub)) as client:
        got = events(client.post("/invocations", json=body, headers=SESSION).text)
    assert [e["type"] for e in got] == ["start", "end"]
    assert got[0]["fresh"] is True and got[0]["v"] == protocol.VERSION
    outputs = got[1]["outputs"]
    assert outputs["release"] == protocol.release() and outputs["protocol"] == protocol.VERSION
    assert calls.state(hub, "rc_hello") == "ended"


def test_warm_files_reads_instructions_and_code_but_never_secrets_or_state(tmp_path):
    hub_dir = tmp_path / "h"
    for rel in ("AGENTS.md", "knowledge/a.md", "tools_local/t.py", "schedule/s.yaml",
                ".env", "restricted/key.md", "output/x/report.md", ".hubzoid/state.json",
                "raw_data/big.txt", "logo.png"):
        path = hub_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    os.symlink(hub_dir / "AGENTS.md", hub_dir / "link.md")
    found = sorted(str(p.relative_to(hub_dir)) for p in server._warm_files(hub_dir))
    assert found == ["AGENTS.md", "knowledge/a.md", "schedule/s.yaml", "tools_local/t.py"]


def test_warm_disk_reads_the_hub_and_the_sdk(tmp_path, fake_sdk, monkeypatch):
    hub_dir = tmp_path / "h"
    hub_dir.mkdir()
    (hub_dir / "AGENTS.md").write_text("hi")
    (fake_sdk / "bundle.js").write_text("y" * 10)
    opened = []
    real_open = type(hub_dir).open

    def spy(self, *a, **kw):
        opened.append(self.name)
        return real_open(self, *a, **kw)

    monkeypatch.setattr(type(hub_dir), "open", spy)
    server._warm_disk(hub_dir)
    assert {"AGENTS.md", "bundle.js", "__init__.py"} <= set(opened)


def test_cli_first_run_reports_what_happened(fake_sdk):
    assert server._cli_first_run() == {"ran": False, "why": "no Claude CLI in claude_agent_sdk"}
    cli = fake_sdk / "_bundled" / "claude"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\necho '2.1.285 (Claude Code)'\n")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    out = server._cli_first_run()
    assert out["ran"] is True and out["code"] == 0 and out["out"] == "2.1.285 (Claude Code)"
