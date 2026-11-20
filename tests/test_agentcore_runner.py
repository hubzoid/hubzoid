"""Agent turns on an AgentCore runner (hubzoid.runner).

The default path must not change: with HUBZOID_RUNNER unset nothing routes to
a runner and the runner package is not imported. With it set, a real runner
(``hubzoid runner serve`` in a subprocess, the scripted test runtime, the same
operational database) serves turns over the AgentCore HTTP contract, and work
orders decide what it may run.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hubzoid import _request_ctx, runtime
from hubzoid.run_events import Notice, ToolCall, ToolResult


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = tmp_path / "runner-hub"
    (hub / "knowledge").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: runner\ndescription: d\n---\nHelp.\n")
    (hub / "knowledge" / "policy.md").write_text("---\nname: policy\ndescription: p\n---\nReturns in 30 days.\n")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("MODEL", "hubzoid-test/scripted")
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "1")
    monkeypatch.setenv("HUBZOID_TEST_SLOW_SECONDS", "6")
    for key in ("HUBZOID_RUNNER", "HUBZOID_ROLE", "HUBZOID_AGENTCORE_ENDPOINT", "HUBZOID_AGENTCORE_RUNTIME_ARN",
                "SHOW_TOOLS", "SHOW_THINKING", "HUBZOID_PUBLIC_URL", "WEBUI_URL"):
        monkeypatch.delenv(key, raising=False)
    return hub


# -- the default path is untouched -------------------------------------------------
def test_default_build_is_unchanged_and_never_imports_the_runner(hub):
    for name in [m for m in sys.modules if m.startswith("hubzoid.runner")]:
        del sys.modules[name]
    rt = runtime.build(hub)
    assert type(rt).__name__ == "ScriptedRuntime"
    assert not [m for m in sys.modules if m.startswith("hubzoid.runner")]


def test_local_flag_keeps_a_build_here(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_RUNNER", "agentcore")
    monkeypatch.setenv("HUBZOID_AGENTCORE_ENDPOINT", "http://127.0.0.1:9")
    assert type(runtime.build(hub)).__name__ == "AgentCoreRuntime"
    assert type(runtime.build(hub, local=True)).__name__ == "ScriptedRuntime"


def test_inside_a_runner_turns_run_locally(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_RUNNER", "agentcore")
    monkeypatch.setenv("HUBZOID_ROLE", "runner")
    assert type(runtime.build(hub)).__name__ == "ScriptedRuntime"


def test_unknown_runner_value_is_refused(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_RUNNER", "lambda")
    with pytest.raises(ValueError, match="local"):
        runtime.build(hub)


# -- protocol ------------------------------------------------------------------------
def test_events_round_trip():
    from hubzoid.run_events import ReasoningDelta
    from hubzoid.runner import protocol

    items = ["hello", ToolCall(id="t1", name="read_knowledge", args={"name": "policy"}),
             ToolResult(id="t1", name="read_knowledge", ok=True, card={"k": 1}),
             ReasoningDelta(text="hm"), Notice(kind="error", text="no")]
    for item in items:
        assert protocol.decode_item(json.loads(protocol.sse(protocol.encode_item(item))[6:])) == item
    assert protocol.encode_item("") is None


def test_session_ids_meet_agentcore_rules():
    import re

    from hubzoid.runner import protocol

    for hub, key in (("support", "web-abc"), ("a" * 40, "x" * 500), ("ops", "md:task:manual-1@app")):
        sid = protocol.session_id(hub, key)
        assert 33 <= len(sid) <= 100 and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9\-_]*", sid)
    assert protocol.session_id("h", "a") != protocol.session_id("h", "b")


def test_sse_parser_handles_split_chunks_and_keepalives():
    from hubzoid.runner import protocol

    raw = protocol.KEEPALIVE + protocol.sse({"type": "text", "text": "a"}) + protocol.sse({"type": "end"})
    events = list(protocol.iter_sse(raw[i:i + 7] for i in range(0, len(raw), 7)))
    assert [e["type"] for e in events] == ["keepalive", "text", "end"]


# -- work orders -------------------------------------------------------------------------
def test_a_work_order_is_claimed_once_and_only_for_its_request(hub):
    from hubzoid.runner import calls

    calls.create(hub, call_id="rc_1", hub="h", kind="turn", session_id="s1", request_digest="d1",
                 identity={"user": "a@example.com"})
    assert calls.claim(hub, call_id="rc_1", request_digest="other", session_id="s1", runner_version="v") is None
    assert calls.claim(hub, call_id="rc_1", request_digest="d1", session_id="other", runner_version="v") is None
    row = calls.claim(hub, call_id="rc_1", request_digest="d1", session_id="s1", runner_version="v")
    assert row["identity"] == {"user": "a@example.com"} and calls.running(hub, "rc_1")
    assert calls.claim(hub, call_id="rc_1", request_digest="d1", session_id="s1", runner_version="v") is None
    assert calls.request_cancel(hub, "rc_1") and not calls.running(hub, "rc_1")
    calls.end(hub, "rc_1", outcome="cancelled")
    assert calls.state(hub, "rc_1") == "ended" and not calls.request_cancel(hub, "rc_1")


def test_a_runner_never_migrates(tmp_path, monkeypatch):
    from sqlalchemy import create_engine

    from hubzoid import migrations

    monkeypatch.setenv("HUBZOID_ROLE", "runner")
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with pytest.raises(migrations.SchemaError, match="hub service"):
        migrations.upgrade(eng, "operational")
    monkeypatch.delenv("HUBZOID_ROLE")
    migrations.upgrade(eng, "operational")          # the hub service migrates
    monkeypatch.setenv("HUBZOID_ROLE", "runner")
    migrations._done.clear()
    migrations.upgrade(eng, "operational")          # current: accepted, no lock


# -- a real runner over HTTP -----------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def runner(hub, monkeypatch, tmp_path):
    from hubzoid import migrations, db

    migrations.upgrade(db.operational_engine(hub), "operational")   # the hub service migrated first
    port = _free_port()
    env = dict(os.environ, HUBZOID_ROLE="runner")
    env.pop("HUBZOID_RUNNER", None)
    proc = subprocess.Popen([sys.executable, "-m", "hubzoid", "runner", "serve", str(hub), "--port", str(port)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/ping", timeout=1).json()["status"] == "Healthy":
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("runner did not start: " + (proc.stdout.read() or b"").decode()[-2000:])
    monkeypatch.setenv("HUBZOID_RUNNER", "agentcore")
    monkeypatch.setenv("HUBZOID_AGENTCORE_ENDPOINT", f"http://127.0.0.1:{port}")
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _turn(hub, prompt, chat="web-chat-1", user="person@example.com"):
    from hubzoid.access import Identity, identity_scope

    async def go():
        rt = runtime.build(hub)
        assert type(rt).__name__ == "AgentCoreRuntime"
        with _request_ctx.chat_scope(chat), identity_scope(Identity.make(user, surface="web")):
            items = [i async for i in rt.stream_events(prompt)]
            return items, _request_ctx.drain_usage(), rt

    return asyncio.run(go())


def test_a_turn_runs_on_the_runner(hub, runner):
    from hubzoid.runner import calls

    items, usage, rt = _turn(hub, "hello there")
    text = "".join(i for i in items if isinstance(i, str))
    assert "hello there" in text and rt.last_error is None, (items, usage, rt.last_error)
    assert usage.get("input_tokens") is not None           # the runner's usage came back
    with calls._engine(hub).connect() as c:
        from sqlalchemy import text as sql

        rows = c.execute(sql("SELECT state, outcome, identity FROM hz_runner_calls")).mappings().all()
    assert rows[-1]["state"] == "ended" and rows[-1]["outcome"] == "ok"
    assert json.loads(rows[-1]["identity"])["user"] == "person@example.com"


def test_a_tool_call_and_its_result_stream_back(hub, runner):
    items, _usage, _rt = _turn(hub, "tool policy please")
    calls_ = [i for i in items if isinstance(i, ToolCall)]
    results = [i for i in items if isinstance(i, ToolResult)]
    assert calls_ and results and results[0].ok


def test_a_failing_turn_reports_its_error(hub, runner):
    items, _usage, rt = _turn(hub, "error now")
    assert any(isinstance(i, Notice) and i.kind == "error" for i in items) or rt.last_error is not None


def test_a_changed_request_is_refused(hub, runner):
    from hubzoid.runner import calls, protocol

    request = {"v": 1, "kind": "turn", "prompt": "hi", "context": {}, "options": {}, "call": "rc_forged"}
    calls.create(hub, call_id="rc_forged", hub=hub.name, kind="turn", session_id="hz-x-" + "0" * 40,
                 request_digest=protocol.digest(request), identity={"user": "a@example.com"})
    request["prompt"] = "something else"          # altered after the hub service recorded it
    r = httpx.post(f"http://127.0.0.1:{runner}/invocations", json=request, timeout=30,
                   headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "hz-x-" + "0" * 40})
    assert '"refused"' in r.text
    assert calls.state(hub, "rc_forged") == "ended"


def test_a_call_with_no_work_order_is_refused(hub, runner):
    r = httpx.post(f"http://127.0.0.1:{runner}/invocations", timeout=30,
                   json={"v": 1, "kind": "turn", "prompt": "hi", "context": {}, "options": {}, "call": "rc_none"},
                   headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "hz-x-" + "1" * 40})
    assert '"refused"' in r.text


def test_cancelling_here_stops_the_turn_there(hub, runner):
    from hubzoid.access import Identity, identity_scope
    from hubzoid.runner import calls

    async def go():
        rt = runtime.build(hub)
        with _request_ctx.chat_scope("web-slow"), identity_scope(Identity.make("p@example.com", surface="web")):
            task = asyncio.create_task(_drain(rt.stream_events("slow please")))
            await asyncio.sleep(2.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def _drain(stream):
        async for _ in stream:
            pass

    asyncio.run(go())
    deadline = time.time() + 20
    from sqlalchemy import text as sql

    while time.time() < deadline:
        with calls._engine(hub).connect() as c:
            row = c.execute(sql("SELECT state, outcome FROM hz_runner_calls ORDER BY created_at DESC")).first()
        if row and row[0] == "ended":
            break
        time.sleep(0.5)
    assert row[0] == "ended" and row[1] == "cancelled"


def test_hello_answers_without_a_model(hub, runner):
    from hubzoid import settings as settingslib
    from hubzoid.runner.client import hello
    from hubzoid.runner.protocol import VERSION

    out = hello(hub, settingslib.load(hub))
    assert out["protocol"] == VERSION and out["runner_version"]


def test_an_approval_wait_reaches_the_hub_service(hub, runner):
    from hubzoid.access import Identity, identity_scope

    (hub / "hitl.yaml").write_text("version: 1\ntools:\n  reserve_stock: {ask: always}\n")

    async def go():
        rt = runtime.build(hub)
        slot = _request_ctx.SessionSlot()
        with _request_ctx.chat_scope("web-approve"), identity_scope(Identity.make("p@example.com", surface="web")), \
                _request_ctx.native_session_scope(slot):
            items = [i async for i in rt.stream_events("reserve please")]
        return items, slot

    items, slot = asyncio.run(go())
    text = "".join(i for i in items if isinstance(i, str))
    assert "Waiting for your approval" in text, items
    notes = getattr(slot, "hitl_waiting", None) or []
    assert notes and notes[0]["tool"] == "reserve_stock" and notes[0]["request_id"]
    from hubzoid.hitl import gate, service

    assert service.get(hub, gate.hub_id_of(hub), notes[0]["request_id"]) is not None   # stored where the hub service resolves it


# -- MCP on AgentCore ------------------------------------------------------------------------
def test_the_mcp_runtime_answers_health_pings_and_nothing_else_unsigned(hub):
    from starlette.testclient import TestClient

    from hubzoid.runner.mcp_app import create_app

    with TestClient(create_app(hub)) as client:
        assert client.get("/ping").json() == {"status": "Healthy"}
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "ping"})
        assert r.status_code == 200 and r.json() == {"jsonrpc": "2.0", "id": 7, "result": {}}
        assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
        assert client.get("/mcp").status_code == 401


def test_mcp_tokens_are_bound_to_the_address_they_were_issued_for(hub):
    from hubzoid.mcp_oauth import HubOAuth

    here, there = "https://hub.example.com/mcp", "https://bedrock-agentcore.ap-southeast-2.amazonaws.com/runtimes/x/invocations"
    service_side = HubOAuth(hub, here, extra_resources=(there,))
    runner_side = HubOAuth(hub, here, extra_resources=(there,))
    runner_side.accept = (there,)
    assert service_side.resources == (here, there)
    single = HubOAuth(hub, here)
    assert single.resources == (here,) and single.accept == (here,)


# -- the image's start command and the AWS provider ---------------------------------------
@pytest.mark.parametrize("role, expect", [("", ["run"]), ("runner", ["runner", "serve"]), ("mcp", ["runner", "mcp"])])
def test_start_picks_what_to_run_from_the_role(hub, monkeypatch, role, expect):
    from typer.testing import CliRunner

    from hubzoid import cli

    calls_ = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls_.append(argv))
    monkeypatch.setenv("HUBZOID_ROLE", role)
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("HUBZOID_OTEL", "off")
    r = CliRunner().invoke(cli.app, ["runner", "start"])
    assert r.exit_code == 0, r.output
    argv = calls_[0]
    assert argv[1:1 + len(expect)] == expect and argv[-1] == str(hub.resolve())


def test_start_refuses_an_unknown_role(hub, monkeypatch):
    from typer.testing import CliRunner

    from hubzoid import cli

    monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("must not start"))
    monkeypatch.setenv("HUBZOID_ROLE", "worker")
    assert CliRunner().invoke(cli.app, ["runner", "start", str(hub)]).exit_code != 0


def test_agentcore_url_encodes_the_arn():
    from hubzoid.runner.protocol import agentcore_url

    arn = "arn:aws:bedrock-agentcore:ap-southeast-2:123456789012:runtime/hub_mcp-AbC123xyZ9"
    assert agentcore_url(arn) == ("https://bedrock-agentcore.ap-southeast-2.amazonaws.com/runtimes/"
                                  "arn%3Aaws%3Abedrock-agentcore%3Aap-southeast-2%3A123456789012%3Aruntime"
                                  "%2Fhub_mcp-AbC123xyZ9/invocations?qualifier=DEFAULT")


def test_the_mcp_runtime_finds_its_own_address_by_name(monkeypatch):
    import boto3

    from hubzoid.runner import mcp_app

    arn = "arn:aws:bedrock-agentcore:ap-southeast-2:123456789012:runtime/hz_mcp-AbC123xyZ9"

    class Ctl:
        def get_paginator(self, _):
            return self

        def paginate(self):
            return [{"agentRuntimes": [{"agentRuntimeName": "other_mcp", "agentRuntimeArn": "x"},
                                       {"agentRuntimeName": "hz_mcp", "agentRuntimeArn": arn}]}]

    monkeypatch.setattr(boto3, "client", lambda *a, **k: Ctl())
    monkeypatch.setenv("HUBZOID_MCP_RUNTIME_NAME", "hz_mcp")
    monkeypatch.delenv("HUBZOID_MCP_AGENTCORE_URL", raising=False)
    from types import SimpleNamespace

    settings = SimpleNamespace(mcp_agentcore_url="", agentcore_region="ap-southeast-2", agentcore_qualifier="DEFAULT")
    assert mcp_app.own_url(settings).endswith("hz_mcp-AbC123xyZ9/invocations?qualifier=DEFAULT")
    settings.mcp_agentcore_url = "https://given.example/x"
    assert mcp_app.own_url(settings) == "https://given.example/x"


def test_deploy_init_aws_writes_the_stack_and_its_image(tmp_path):
    from hubzoid.deploy import scaffold

    hub = tmp_path / "order-desk"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: o\ndescription: d\n---\n")
    written = {w.path for w in scaffold.write(hub, "aws")}
    assert written == {"Dockerfile", ".dockerignore", "deploy/aws/README.md", "deploy/aws/stack.yaml"}
    docker = (hub / "Dockerfile").read_text()
    assert 'CMD ["runner", "start"]' in docker and "COPY . /hubs/order-desk" in docker
    stack = (hub / "deploy" / "aws" / "stack.yaml").read_text()
    assert 'Default: "order-desk"' in stack and 'Default: "order_desk"' in stack and "{{" not in stack.replace("{{resolve", "")
    # The other providers are unchanged: the common Dockerfile and no stack.
    assert set(scaffold.render("railway")) == {"Dockerfile", ".dockerignore", "deploy/railway/README.md",
                                               "deploy/railway/env.example"}
    assert 'CMD ["run", "/hubs/hub"]' in scaffold.render("railway")["Dockerfile"]


# -- review fixes ---------------------------------------------------------------------------
def test_the_cli_starts_without_the_runner_package():
    code = ("import sys, hubzoid.cli; "
            "print(sorted(m for m in sys.modules if m.startswith('hubzoid.runner') and m != 'hubzoid.runner_cli'))")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUBZOID_")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"


def test_a_call_its_hub_service_stopped_renewing_may_not_go_on(hub):
    import time as _t

    from sqlalchemy import text as sql

    from hubzoid.runner import calls

    calls.create(hub, call_id="rc_hb", hub="h", kind="turn", session_id="s", request_digest="d", identity=None)
    calls.claim(hub, call_id="rc_hb", request_digest="d", session_id="s", runner_version="v")
    assert calls.running(hub, "rc_hb") and calls.beat(hub, "rc_hb")
    with calls._engine(hub).begin() as c:
        c.execute(sql("UPDATE hz_runner_calls SET heartbeat_at=:t WHERE id='rc_hb'"),
                  {"t": _t.time() - calls.LIVE_SECONDS - 1})
    assert not calls.running(hub, "rc_hb")
    calls.end(hub, "rc_hb", outcome="ok")
    assert not calls.beat(hub, "rc_hb")


def test_a_call_for_another_release_is_refused(hub, runner):
    from hubzoid.runner import calls, protocol

    request = {"v": 1, "kind": "turn", "prompt": "hi", "context": {}, "options": {}, "call": "rc_old",
               "release": {"hubzoid": "1.0.0", "image": ""}}
    calls.create(hub, call_id="rc_old", hub=hub.name, kind="turn", session_id="hz-x-" + "2" * 40,
                 request_digest=protocol.digest(request), identity={"user": "a@example.com"})
    r = httpx.post(f"http://127.0.0.1:{runner}/invocations", json=request, timeout=30,
                   headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "hz-x-" + "2" * 40})
    assert '"refused"' in r.text and '"version"' in r.text
    assert protocol.release_mismatch({"hubzoid": "a"}, {"hubzoid": "a", "image": "x"}) == ""
    assert protocol.release_mismatch({"hubzoid": "a", "image": "y"}, {"hubzoid": "a", "image": "x"})


def test_a_turn_that_fails_before_it_starts_ends_with_a_visible_error(hub, runner):
    from hubzoid import run_events

    async def go():
        rt = runtime.build(hub, bundle={"kind": "schedule", "task": "missing", "emit": None, "overrides": {}})
        from hubzoid.access import Identity, identity_scope

        with _request_ctx.chat_scope("web-broken"), identity_scope(Identity.make("p@example.com", surface="web")):
            t0 = time.monotonic()
            items = [i async for i in rt.stream_events("hello")]
            text = "".join([t async for t in run_events.as_text(_replay(items))])
            return items, text, rt, time.monotonic() - t0

    async def _replay(items):
        for i in items:
            yield i

    items, text, rt, seconds = asyncio.run(go())
    assert seconds < 30 and rt.last_error is not None
    assert any(isinstance(i, Notice) and i.kind == "error" for i in items)
    assert "AgentCore" in text                       # text-only callers (/v1, channels) see it too


def test_the_disk_warmup_never_reads_configuration_or_restricted_files(tmp_path):
    from hubzoid.runner.server import _warm_files

    hub = tmp_path / "h"
    for rel in ("AGENTS.md", "knowledge/a.md", "tools_local/t.py", ".env", "restricted/.env", "restricted/k.md",
                ".hubzoid/secret.key", "output/x.md", "deploy/aws/stack.yaml", "secrets.pem"):
        (hub / rel).parent.mkdir(parents=True, exist_ok=True)
        (hub / rel).write_text("x")
    got = sorted(str(p.relative_to(hub)) for p in _warm_files(hub))
    assert got == ["AGENTS.md", "knowledge/a.md", "tools_local/t.py"]


def test_a_scheduled_task_keeps_its_run_identity_and_scratch_on_the_runner(hub, monkeypatch):
    from hubzoid.runner import server
    from hubzoid.tools import schedule_tools

    (hub / "schedule").mkdir()
    (hub / "schedule" / "daily.md").write_text("---\nschedule: \"0 3 * * *\"\n---\nDo it.\n")
    seen = {}
    monkeypatch.setattr(schedule_tools, "make", lambda hub_dir, task, emit: seen.setdefault("task", task) and [])
    server._schedule_tools(hub, {"task": "daily", "overrides": {"timeout": 99},
                                 "run": {"state_rel": ".hubzoid/state/acct/daily", "run_id": "wf-1",
                                         "run_identity": {"subject": "a@example.com"}}}, lambda **k: None)
    task = seen["task"]
    assert (task.state_rel, task.run_id, task.run_identity, task.timeout) == (
        ".hubzoid/state/acct/daily", "wf-1", {"subject": "a@example.com"}, 99)


def test_stopping_a_call_already_asked_to_stop_still_ends_it(hub, monkeypatch):
    from hubzoid.runner import calls, client

    stopped = []

    class Tp:
        def stop_session(self, sid):
            stopped.append(sid)

    monkeypatch.setattr(client, "transport", lambda settings: Tp())
    monkeypatch.setattr(client, "STOP_GRACE_SECONDS", 0.5)
    calls.create(hub, call_id="rc_st", hub="h", kind="turn", session_id="s-st", request_digest="d", identity=None)
    calls.claim(hub, call_id="rc_st", request_digest="d", session_id="s-st", runner_version="v")
    calls.request_cancel(hub, "rc_st")              # the chat run's guard got there first
    rt = client.AgentCoreRuntime(hub, None, model_id="m")
    rt._stop_call("rc_st", "s-st")
    assert stopped == ["s-st"] and calls.state(hub, "rc_st") == "ended"
    rt._stop_call("rc_st", "s-st")                  # again: nothing more happens
    assert stopped == ["s-st"]


def test_sign_in_finds_the_mcp_runtime_once_it_exists(hub, monkeypatch):
    from hubzoid import mcp_oauth

    url = ("https://bedrock-agentcore.ap-southeast-2.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore"
           "%3Aap-southeast-2%3A1%3Aruntime%2Fh_mcp-x/invocations?qualifier=DEFAULT")
    answers = iter(["", url])
    clock = [1000.0]
    monkeypatch.setattr(mcp_oauth.time, "time", lambda: clock[0])
    oauth = mcp_oauth.HubOAuth(hub, "https://hub.example.com/mcp", find_extra=lambda: next(answers))
    assert oauth.resources == ("https://hub.example.com/mcp",)        # not created yet
    assert oauth.resources == ("https://hub.example.com/mcp",)        # not asked again within a minute
    clock[0] += 61
    assert oauth.resources == ("https://hub.example.com/mcp", url)    # found, and kept
    assert oauth.accept == ("https://hub.example.com/mcp",)           # this endpoint still takes only its own
