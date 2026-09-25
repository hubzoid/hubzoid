"""Codex transport, isolation and shared tool contract (no paid model calls)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agents import function_tool

from hubzoid import _request_ctx
from hubzoid.factory_codex import CodexRuntime


class Process:
    def __init__(self, events):
        self.stdout = asyncio.StreamReader()
        for event in events:
            self.stdout.feed_data((json.dumps(event) + "\n").encode())
        self.stdout.feed_eof()
        self.stdin = self
        self.messages = []

    def write(self, value):
        self.messages.append(json.loads(value))

    async def drain(self):
        pass


def events(*middle):
    return [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"thread": {"id": "thread"}, "model": "example-model"}},
        *middle,
        {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"status": "completed"}}},
    ]


@pytest.mark.asyncio
async def test_protocol_tool_identity_usage_and_no_environment():
    from hubzoid.access import Identity, identity_scope, current_identity
    called = []

    @function_tool
    def who() -> str:
        """Return caller."""
        called.append(current_identity().user)
        return current_identity().user

    proc = Process(events(
        {"method": "item/tool/call", "id": 10, "params": {"tool": "who", "arguments": {}, "callId": "call", "threadId": "thread"}},
        {"method": "item/agentMessage/delta", "params": {"itemId": "a", "delta": "Hello"}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "a", "text": "Hello"}}},
        {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {"inputTokens": 12, "outputTokens": 3}}}},
    ))
    rt = CodexRuntime(name="demo", instructions="Use hub tools", registry={"who": who}, tool_mode="off")
    usage = {}
    with identity_scope(Identity.make("person@example.org", surface="mcp")):
        result = "".join([x async for x in rt._exchange(proc, "hi", "/tmp/empty", usage)])
    assert result == "Hello"
    assert called == ["person@example.org"]
    start = next(m["params"] for m in proc.messages if m.get("method") == "thread/start")
    assert start["environments"] == [] and start["selectedCapabilityRoots"] == []
    assert start["ephemeral"] and start["allowProviderModelFallback"] is False
    assert start["dynamicTools"][0]["inputSchema"] == who.params_json_schema
    assert usage == {"model": "example-model", "input_tokens": 12, "output_tokens": 3}
    assert next(m for m in proc.messages if m.get("id") == 10)["result"]["contentItems"][0]["text"] == "person@example.org"


@pytest.mark.asyncio
async def test_unknown_tool_and_native_approval_are_refused():
    proc = Process(events(
        {"id": 10, "method": "item/tool/call", "params": {"tool": "exec_command", "arguments": {}, "callId": "c"}},
        {"id": 11, "method": "item/commandExecution/requestApproval", "params": {}},
    ))
    rt = CodexRuntime(name="test", instructions="", registry={})
    _ = [x async for x in rt._exchange(proc, "hi", "/tmp", {})]
    assert next(m for m in proc.messages if m.get("id") == 10)["result"]["success"] is False
    assert "error" in next(m for m in proc.messages if m.get("id") == 11)


@pytest.mark.asyncio
async def test_tool_budget_stops_loop():
    call = {"id": 10, "method": "item/tool/call", "params": {"tool": "missing", "arguments": {}, "callId": "c"}}
    proc = Process(events(call, call))
    rt = CodexRuntime(name="test", instructions="", registry={}, max_turns=1)
    with pytest.raises(RuntimeError, match="tool-call limit"):
        _ = [x async for x in rt._exchange(proc, "hi", "/tmp", {})]


@pytest.mark.asyncio
async def test_failure_and_wrong_thread_fail_closed():
    for event in [
        {"method": "turn/completed", "params": {"turn": {"status": "failed"}}},
        {"method": "item/agentMessage/delta", "params": {"threadId": "someone-else", "delta": "secret"}},
        {"id": 3, "error": {"message": "secret"}},
    ]:
        rt = CodexRuntime(name="test", instructions="", registry={})
        with pytest.raises(RuntimeError):
            _ = [x async for x in rt._exchange(Process(events(event)), "hi", "/tmp", {})]


def test_routing_and_delegates(tmp_path, monkeypatch):
    from hubzoid import runtime, handover, factory_codex
    (tmp_path / "AGENTS.md").write_text("---\nname: demo\nmodel: codex-local\n---\nHello")
    monkeypatch.delenv("MODEL", raising=False)
    built = runtime.build(tmp_path)
    assert isinstance(built, CodexRuntime)
    assert "read_file" in built.registry
    assert json.loads(runtime.describe(tmp_path))["backend"] == "codex-local"
    assert handover.engine("codex-local/x") == "codex"
    assert handover.classify("codex-local/y", "codex-local/x") == "delegate"
    assert handover.classify("gpt-4o", "codex-local") == "skill"


def test_workflow_structured_completion(tmp_path, monkeypatch):
    from hubzoid import runtime
    async def stream(self, prompt):
        assert self.registry == {}
        _request_ctx.record_usage({"input_tokens": 5, "output_tokens": 2})
        yield '{"ok":true}'
    monkeypatch.setattr(CodexRuntime, "stream", stream)
    result = runtime.complete_once(tmp_path, {"model": "codex-local", "prompt": "json", "response_format": "json"})
    assert result["json"] == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [False, True])
async def test_real_codex_has_only_hub_tools(tmp_path, monkeypatch, code_mode):
    """Real CLI + local fake Responses API: inspect the actual model tool list.

    No provider credentials or paid model call. Skips if the audited CLI is absent.
    """
    import shutil
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from hubzoid import factory_codex
    try:
        binary = factory_codex.codex_binary()
    except RuntimeError:
        pytest.skip("audited Codex CLI not installed")
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests) == 1:
                item = ({"id": "fc1", "type": "custom_tool_call", "name": "exec", "call_id": "call1",
                         "input": 'text({process:typeof process,require:typeof require,fetch:typeof fetch}); try { await import("node:fs"); } catch(e) { text("import blocked"); } if (ALL_TOOLS.length !== 1 || ALL_TOOLS[0].name !== "sample") throw new Error("unexpected tool exposure"); text(await tools.sample({}));'}
                        if code_mode else {"id": "fc1", "type": "function_call", "name": "sample", "call_id": "call1", "arguments": "{}"})
            else:
                item = {"id": "msg1", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done", "annotations": []}], "status": "completed"}
            events_ = [
                {"type": "response.created", "response": {"id": f"r{len(requests)}"}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": f"r{len(requests)}", "status": "completed", "output": [item], "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}},
            ]
            body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events_).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    auth = tmp_path / "auth.json"
    auth.write_text('{}')
    monkeypatch.setattr(factory_codex, "_auth_file", lambda: auth)
    original = asyncio.create_subprocess_exec
    captured = {}
    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        extra = ["-c", 'model_provider="fixture"', "-c", 'model="fixture-model"',
                 "-c", f'model_providers.fixture={{name="Fixture",base_url="http://127.0.0.1:{server.server_port}/v1",wire_api="responses",requires_openai_auth=false}}']
        if code_mode:
            extra += ["-c", "features.code_mode_only=true"]
        return await original(*args, *extra, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    called = []
    @function_tool
    def sample() -> str:
        """A synthetic sample."""
        called.append(True)
        return "fictional data"
    rt = CodexRuntime(name="test", instructions="Use sample.", registry={"sample": sample}, tool_mode="off")
    try:
        result = await asyncio.wait_for(rt.run("Call sample then answer Done."), timeout=45)
        assert rt.last_error is None, result
        assert result == "Done"
        assert called == [True]
        assert len(requests) == 2
        if code_mode:
            assert {t.get("name") for t in requests[0]["tools"]} <= {"exec", "wait"}, requests[0]["tools"]
            output = json.dumps(requests[1]["input"])
            assert "import blocked" in output
            assert output.count("undefined") >= 3
        else:
            assert {t.get("name") for t in requests[0]["tools"]} == {"sample"}, requests[0]["tools"]
            assert requests[0]["tools"][0]["parameters"] == {k: v for k, v in sample.params_json_schema.items() if k != "title"}
        assert "fictional data" in json.dumps(requests[1]["input"])
        assert not Path(captured["env"]["CODEX_HOME"]).exists()
        assert not rt._processes
    finally:
        await rt.aclose()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_auth_refresh_preserves_concurrent_login(tmp_path):
    from hubzoid.factory_codex import _persist_refreshed_auth
    original, temporary = tmp_path / "auth.json", tmp_path / "copy.json"
    original.write_bytes(b'old')
    temporary.write_bytes(b'refreshed')
    _persist_refreshed_auth(original, temporary, b'old')
    assert original.read_bytes() == b'refreshed'
    assert original.stat().st_mode & 0o777 == 0o600
    original.write_bytes(b'new-operator-login')
    _persist_refreshed_auth(original, temporary, b'old')
    assert original.read_bytes() == b'new-operator-login'


@pytest.mark.asyncio
async def test_cancellation_terminates_child_and_removes_credentials(tmp_path, monkeypatch):
    from hubzoid import factory_codex
    auth = tmp_path / "auth.json"
    auth.write_text('{}')
    monkeypatch.setattr(factory_codex, "_auth_file", lambda: auth)
    monkeypatch.setattr(factory_codex, "codex_binary", lambda: "unused")
    started = asyncio.Event()
    class Child:
        returncode = None
        pid = 99999
    child = Child()
    captured = {}
    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return child
    stopped = []
    async def stop(proc):
        stopped.append(proc)
        proc.returncode = -15
    async def exchange(self, proc, prompt, cwd, usage):
        started.set()
        await asyncio.Future()
        yield "unreachable"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(factory_codex, "_stop", stop)
    monkeypatch.setattr(CodexRuntime, "_exchange", exchange)
    rt = CodexRuntime(name="test", instructions="", registry={})
    task = asyncio.create_task(rt.run("hello"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == [child]
    assert not Path(captured["env"]["CODEX_HOME"]).exists()
    assert not rt._processes


@pytest.mark.asyncio
async def test_guarded_tool_denies_ungranted_caller(tmp_path):
    from hubzoid.access import Identity, identity_scope
    from hubzoid.access.guard import guard_tool
    called = []
    @function_tool
    def confidential() -> str:
        """Restricted synthetic data."""
        called.append(True)
        return "should not be exposed"
    guarded = guard_tool(confidential, "reports", tmp_path)
    proc = Process(events({"method": "item/tool/call", "id": 10, "params": {"tool": "confidential", "arguments": {}, "callId": "call", "threadId": "thread"}}))
    rt = CodexRuntime(name="demo", instructions="", registry={"confidential": guarded}, tool_mode="off")
    with identity_scope(Identity.make("person@example.org", surface="mcp")):
        _ = [part async for part in rt._exchange(proc, "show report", "/tmp", {})]
    assert not called
    assert "denied" in json.dumps(next(m for m in proc.messages if m.get("id") == 10)).lower()


def test_delegate_usage_is_combined_without_pricing_as_the_wrong_model(monkeypatch):
    from hubzoid.factory_codex import _combine_usage
    from hubzoid import usage
    monkeypatch.setattr(usage, "estimate_cost", lambda model, inp, out: {"parent": .01, "child": .02}.get(model))
    rows = [{"model": "parent", "input_tokens": 10, "output_tokens": 2},
            {"model": "child", "input_tokens": 20, "output_tokens": 3}]
    assert _combine_usage(rows) == {"model": None, "input_tokens": 30, "output_tokens": 5, "cost_usd": .03}
    rows[1]["model"] = "unknown"
    assert _combine_usage(rows)["cost_usd"] is None
