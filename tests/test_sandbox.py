"""Sandboxes: files, station use and, with Docker, the real lifecycle.

The Docker test is marked slow and skips without a reachable Docker engine.
The milestone's test-hub run covers real Claude and Codex stations in a
sandbox.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from hubzoid import db
from hubzoid.sandbox import config
from hubzoid.stations import StationError, definitions
from hubzoid.stations import service as stations
from hubzoid.work_items import service
from hubzoid.work_items.errors import WorkItemError
from hubzoid.workflows import context
from hubzoid.workflows.identity import RunIdentity

OWNER = "admin@localhost"
DEPLOYMENT = "version: 1\nsandbox:\n  enabled: true\n  default_provider: local\n  providers:\n    local:\n      kind: docker\n"
PROFILE = textwrap.dedent("""\
    version: 1
    image:
      base: hubzoid-default
    resources: {cpus: 1, memory_mib: 512, pids: 128}
    network:
      setup: {mode: deny_all}
    lifecycle: {idle_stop_seconds: 60, max_lifetime_seconds: 600}
""")


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for var in ("WEBUI_AUTH", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "boxhub"
    (root / "sandboxes").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Box\nmodel: claude-local\n---\nA hub.\n")
    (root / "sandbox.yaml").write_text(DEPLOYMENT)
    (root / "sandboxes" / "work.yaml").write_text(PROFILE)
    folder = root / "agents" / "coder"
    folder.mkdir(parents=True)
    (folder / "AGENTS.md").write_text("---\nname: coder\ndescription: codes\nisolated: true\nengine: claude-local\n"
                                      "sandbox: work\ntools: [exec_command, read_file, edit_file]\n---\nCode.\n")
    (root / "workflows" / "w").mkdir(parents=True)
    (root / "workflows" / "w" / "main.py").write_text(
        "from hubzoid import hub, workflow\nfrom hubzoid.work_items import Route\n\n"
        "@workflow(work_items={'fix': Route(stages=('code',), actions=())})\n"
        "def w():\n    return hub.call_agent('x', agent='coder', stage='code')\n")
    return root


def bad(hub_dir, name, text, profile=True):
    target = hub_dir / "sandboxes" / f"{name}.yaml" if profile else hub_dir / "sandbox.yaml"
    target.write_text(text)
    with pytest.raises(WorkItemError) as err:
        config.load_profile(hub_dir, name) if profile else config.load_deployment(hub_dir)
    return err.value


def test_files_refuse_what_cannot_be_enforced(hub_dir):
    profile = config.load_profile(hub_dir, "work")
    assert profile.network["agent"] == {"mode": "deny_all"} and profile.setup_hosts() == []
    err = bad(hub_dir, "x", PROFILE.replace("setup: {mode: deny_all}", "setup: {mode: deny_all}\n  agent: {mode: allowlist}"))
    assert err.code == "network_mode_unsupported" and err.fields["line"] == 7
    assert bad(hub_dir, "x", PROFILE.replace("deny_all", "allow_all")).code == "network_mode_unsupported"
    assert bad(hub_dir, "x", PROFILE + "env:\n  GITHUB_TOKEN: abc\n").code == "secret_in_profile"
    assert bad(hub_dir, "x", PROFILE + "env:\n  NOTE: ghp_abcdefghijklmnopqrstuvwxyz0123456789\n").code == "secret_in_profile"
    assert "digest" in bad(hub_dir, "x", PROFILE.replace("base: hubzoid-default", "ref: python:3.12")).message
    assert "not supported" in bad(hub_dir, "x", PROFILE.replace("base: hubzoid-default", "dockerfile: D")).message
    assert "inside /workspace" in bad(hub_dir, "x", PROFILE + "setup:\n  command: make\n  cwd: ../etc\n").message
    assert bad(hub_dir, "", DEPLOYMENT.replace("kind: docker", "kind: vercel"), profile=False).code == \
        "provider_not_available"
    assert "TLS" in bad(hub_dir, "", DEPLOYMENT + "      endpoint: tcp://box:2375\n", profile=False).message
    assert "not supported" in bad(hub_dir, "", DEPLOYMENT + "  limits:\n    max_jobs_per_sandbox: 4\n",
                                  profile=False).message
    (hub_dir / "sandbox.yaml").write_text(DEPLOYMENT)
    allow = PROFILE.replace("setup: {mode: deny_all}", "setup: {mode: allowlist, presets: [registries-v1]}")
    (hub_dir / "sandboxes" / "net.yaml").write_text(allow + "setup:\n  command: uv sync --frozen\n")
    assert "pypi.org" in config.load_profile(hub_dir, "net").setup_hosts()
    assert config.load_profile(hub_dir, "net").digest != profile.digest


def test_sandbox_tools_need_a_sandbox(hub_dir):
    from hubzoid.stations import tools

    assert definitions.validate(hub_dir)["valid"]
    text = (hub_dir / "agents" / "coder" / "AGENTS.md").read_text().replace("sandbox: work\n", "")
    (hub_dir / "agents" / "coder" / "AGENTS.md").write_text(text)
    report = definitions.validate(hub_dir)
    assert any(c["code"] == "sandbox_tool_without_sandbox" for c in report["checks"])
    with pytest.raises(WorkItemError):
        tools.for_station(hub_dir, ["exec_command"])
    plain = tools.for_station(hub_dir, ["read_file"])           # no sandbox: still the hub's own reader
    assert plain["read_file"].description != tools.for_station(hub_dir, ["read_file"], sandbox=True)[
        "read_file"].description
    boxed = tools.for_station(hub_dir, ["exec_command", "read_file", "read_hub_file"], sandbox=True)
    assert list(boxed) == ["exec_command", "read_file", "read_hub_file"]
    assert boxed["read_file"] is not boxed["read_hub_file"]          # the hub's file tool keeps working
    from hubzoid import tool_classes

    assert tool_classes.classify(boxed["read_hub_file"]) == "read"     # the alias is still a read


class FakeSandboxes:
    """Records what the station asked of the sandbox service."""

    def __init__(self, monkeypatch, *, lost=False):
        from hubzoid.sandbox import service as sandbox_service

        self.calls, self.rows = [], []
        self.lost = lost
        monkeypatch.setattr(sandbox_service, "allocate", self.allocate)
        monkeypatch.setattr(sandbox_service, "release", lambda hub, sid, reason: self.calls.append(("release", reason)))
        monkeypatch.setattr(sandbox_service, "stop", lambda hub, sid, reason: self.calls.append(("stop", reason)))
        monkeypatch.setattr(sandbox_service, "list_rows", lambda hub, **kw: list(self.rows))
        monkeypatch.setattr(sandbox_service, "get", lambda hub, sid: {"checkouts_json": []})

    def allocate(self, hub_dir, **kw):
        self.calls.append(("allocate", kw["owner_id"], kw.get("checkout")))
        return {"id": "box-1"}


def scope_for(hub_dir, item):
    ident = RunIdentity(OWNER, None, "test", OWNER).to_dict()
    return context.run_scope(hub="boxhub", workflow="w", hub_dir=hub_dir, engine=db.operational_engine(hub_dir),
                             identity=ident, run_id="r", work={"item": item, "stage": None})


def test_a_station_gets_its_own_sandbox_and_releases_it(hub_dir, monkeypatch):
    from hubzoid import runtime

    boxes = FakeSandboxes(monkeypatch)
    item = service.admit(hub_dir, workflow="w", key="k", requester=OWNER)[0]
    seen = []
    with scope_for(hub_dir, item):
        monkeypatch.setattr(runtime, "run_station", _raises(runtime.AgentRunError("model down")))
        with pytest.raises(StationError) as err:
            stations.call("coder", "Fix it.", stage="code", checkout={"url": "https://user:pw@git.example/r.git"})
        assert boxes.calls[-1] == ("stop", "awaiting_retry")      # kept for the call's one retry
        monkeypatch.setattr(runtime, "run_station", _returns({"text": "fixed"}, seen))
        assert stations.retry(err.value.call_id, "Try again.") == {"text": "fixed"}
    assert boxes.calls[-1] == ("release", "completed")
    assert seen[-1] == ["edit_file", "exec_command", "read_file"]
    # The recorded checkout has no credential; the clone uses the host's.
    assert boxes.calls[0][2] == {"url": "https://git.example/r.git"}


def test_a_lost_sandbox_is_not_recreated_on_recovery(hub_dir, monkeypatch):
    boxes = FakeSandboxes(monkeypatch)
    item = service.admit(hub_dir, workflow="w", key="k", requester=OWNER)[0]
    from hubzoid import runtime

    monkeypatch.setattr(runtime, "run_station", _returns({"text": "x"}, []))
    with scope_for(hub_dir, item):
        stations.call("coder", "Fix it.", stage="code")
    call_id = stations.list_calls(hub_dir, item["id"])[0]["id"]
    # The process died mid-attempt, and meanwhile the sandbox disappeared.
    row = stations.get_call(hub_dir, call_id)
    attempts = [dict(row["attempts_json"][0], status="running")]
    stations._update_call(hub_dir, call_id, expected_revision=row["revision"], attempts_json=attempts,
                          status="started", output_json=None)
    boxes.rows = [{"id": "box-1", "state": "lost", "live_key": None}]
    allocations = len([c for c in boxes.calls if c[0] == "allocate"])
    with scope_for(hub_dir, item):
        with pytest.raises(StationError) as err:
            stations._attempt(context._ctx(), {"item": item}, call_id, "initial")
    assert err.value.code == "sandbox_lost"
    assert len([c for c in boxes.calls if c[0] == "allocate"]) == allocations
    # Removed by an operator counts the same: recovery never starts it empty.
    boxes.rows = [{"id": "box-1", "state": "deleted", "live_key": None}]
    stations._update_call(hub_dir, call_id, expected_revision=stations.get_call(hub_dir, call_id)["revision"],
                          attempts_json=attempts, status="started", recovery_count=0)
    with scope_for(hub_dir, item):
        with pytest.raises(StationError) as err:
            stations._attempt(context._ctx(), {"item": item}, call_id, "initial")
    assert err.value.code == "sandbox_lost"
    # An explicit retry is the one way to a fresh sandbox.
    row = stations.get_call(hub_dir, call_id)
    stations._update_call(hub_dir, call_id, expected_revision=row["revision"], attempts_json=[], status="started",
                          retry_used=1, clarification="Start again.")
    with scope_for(hub_dir, item):
        assert stations._attempt(context._ctx(), {"item": item}, call_id, "retry") == {"text": "x"}
    assert len([c for c in boxes.calls if c[0] == "allocate"]) == allocations + 1


def _raises(exc):
    def fn(*a, **kw):
        raise exc
    return fn


def _returns(out, seen):
    def fn(hub, definition, message, *, subject=None, timeout=None, registry=None):
        seen.append(sorted(registry))
        return {"text": json.dumps(out), "output": out, "model": "fake", "usage": {}}
    return fn


def _docker_ok() -> bool:
    try:
        import docker
        docker.from_env(timeout=5).ping()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _docker_ok(), reason="needs a reachable Docker engine")
def test_restart_keeps_files_and_a_deleted_sandbox_is_lost(hub_dir, monkeypatch):
    import asyncio
    import time

    from hubzoid.sandbox import service as sandbox_service
    from hubzoid.sandbox import tools

    async def call(box_tools, name, args):
        return json.loads(await box_tools[name].on_invoke_tool(None, json.dumps(args)))

    # Setup reaches only the allowed registries through the proxy: no direct
    # route, no host gateway, and the proxy refuses private addresses.
    (hub_dir / "sandboxes" / "net.yaml").write_text(PROFILE.replace(
        "setup: {mode: deny_all}", "setup: {mode: allowlist, presets: [registries-v1]}") + textwrap.dedent("""\
        setup:
          command: >-
            curl -s -m 5 -o /dev/null -w '%{http_code}' http://169.254.169.254/latest/ > meta.txt;
            curl -s -m 5 --noproxy '*' -o /dev/null -w '%{http_code}' http://172.17.0.1/ > direct.txt;
            exit 0
    """))
    sandbox_service.prepare(hub_dir, "net")
    net = sandbox_service.probe(hub_dir, "net", ["cat", "meta.txt", "direct.txt"])
    assert net["exit_code"] == 0 and net["stdout"] == "403000" and net["network"] == "deny_all"

    sandbox_service.prepare(hub_dir, "work")
    row = sandbox_service.allocate(hub_dir, owner_kind="probe", owner_id="p1", profile_id="work")
    try:
        assert row["state"] == "ready" and row["network_json"]["effective"] == "deny_all"
        first = tools.make(hub_dir, row["id"])
        assert asyncio.run(call(first, "write_file", {"path": "notes.txt", "content": "kept\n"}))["bytes"] == 5
        sandbox_service.stop(hub_dir, row["id"], reason="idle")
        sandbox_service._PROVIDERS.clear()                     # as after a Hubzoid restart
        again = tools.make(hub_dir, row["id"])
        assert asyncio.run(call(again, "read_file", {"path": "notes.txt"}))["content"].endswith("kept")
        assert asyncio.run(call(again, "write_file", {"path": "notes.txt", "content": "x"}))["bytes"] == 1
        # The agent's own activity does not make an operator's revision stale.
        seen = sandbox_service.get(hub_dir, row["id"])["revision"]
        asyncio.run(call(again, "read_file", {"path": "notes.txt"}))
        assert sandbox_service.operator_stop(hub_dir, row["id"], "cli:admin", reason="check",
                                             expected_revision=seen)["state"] == "stopped"
        # Removed by an administrator: its owner sees it lost, with the reason.
        removed = sandbox_service.allocate(hub_dir, owner_kind="probe", owner_id="p3", profile_id="work")
        sandbox_service.operator_delete(hub_dir, removed["id"], "cli:admin", reason="demo")
        with pytest.raises(WorkItemError) as gone:
            sandbox_service.attach(hub_dir, removed["id"])
        assert gone.value.code == "sandbox_lost" and "administrator" in gone.value.message
        # Deleted out of band: reported lost, never recreated.
        prov = sandbox_service._provider_for(hub_dir, row)
        prov.container(row["provider_json"]).remove(force=True)
        with pytest.raises(WorkItemError) as err:
            sandbox_service.attach(hub_dir, row["id"])
        assert err.value.code == "sandbox_lost" and sandbox_service.get(hub_dir, row["id"])["state"] == "lost"
        # Its leftover network-free resources (none here) and any orphan are cleaned up by label.
        orphan = sandbox_service.allocate(hub_dir, owner_kind="probe", owner_id="p2", profile_id="work")
        sandbox_service._end(hub_dir, orphan["id"], "deleted", "test")        # a row gone, a container left
        later = time.time() + 3600
        monkeypatch.setattr(sandbox_service.time, "time", lambda: later)
        plan = sandbox_service.reap(hub_dir, dry_run=False)
        assert orphan["id"] in {o["id"] for o in plan["orphans"]}
        assert prov.container(orphan["provider_json"]) is None
    finally:
        sandbox_service.release(hub_dir, row["id"], "completed")
