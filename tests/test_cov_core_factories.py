"""Runtime adapter failures, tool audit hooks, read-only hints and credential cleanup."""
from __future__ import annotations

import asyncio
import contextvars
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from hubzoid import agent_errors, audit_log, factory, factory_claude as claude, factory_codex as codex
from hubzoid.access import Identity
from hubzoid.access import evaluator, guard
from hubzoid.embed import scope
from tests.test_codex_runtime import CodexRuntime, Process, events


@pytest.mark.parametrize(("error", "expected"), [
    ({"codexErrorInfo": "rateLimitExceeded"}, agent_errors.USAGE_LIMIT),
    ({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}}, agent_errors.USAGE_LIMIT),
    ({"codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 403}}}, agent_errors.AUTH),
    ({"codexErrorInfo": {"unknown": {"httpStatusCode": 500}}}, None),
    ({"codexErrorInfo": "newKind"}, None),
])
def test_codex_structured_error_mapping(error, expected):
    assert codex._turn_error_kind({"error": error}) == expected


def test_codex_token_claims_and_failed_readonly_detection(monkeypatch):
    for data in ({}, {"tokens": {"id_token": "not.a.token"}}, {"tokens": {"id_token": []}}):
        assert codex._plan_from_login_file(data) is None
    monkeypatch.setattr(codex, "codex_binary", Mock(side_effect=OSError("missing")))
    assert codex.codex_available() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", [1, 9999])
async def test_codex_stop_never_signals_invalid_group_and_escalates_timeout(monkeypatch, pid):
    signals = []
    monkeypatch.setattr(codex, "os", SimpleNamespace(name="posix", killpg=lambda p, sig: signals.append((p, sig))))
    proc = SimpleNamespace(returncode=None, pid=pid, kill=Mock(),
                           wait=AsyncMock(side_effect=[TimeoutError(), None] if pid > 1 else [None]))
    await codex._stop(proc)
    if pid == 1:
        assert signals == []
        proc.kill.assert_called_once()
    else:
        assert [p for p, sig in signals] == [9999, 9999]
        assert signals[0][1] != signals[1][1] and proc.wait.await_count == 2


@pytest.mark.asyncio
async def test_codex_windows_stop_uses_tree_cleanup_and_disappeared_process_is_safe(monkeypatch):
    from hubzoid import _process_tree

    proc = SimpleNamespace(returncode=None, pid=9999, wait=AsyncMock())
    stopped = Mock()
    monkeypatch.setattr(_process_tree, "stop_windows_tree", stopped)
    monkeypatch.setattr(codex, "os", SimpleNamespace(name="nt"))
    await codex._stop(proc)
    stopped.assert_called_once_with(proc)
    proc.wait.assert_awaited_once()
    monkeypatch.setattr(codex, "os", SimpleNamespace(name="posix", killpg=Mock(side_effect=ProcessLookupError)))
    await codex._stop(proc)
    proc.returncode = 0
    await codex._stop(proc)
    assert proc.wait.await_count == 1


def test_credential_refresh_failure_is_logged_without_overwriting_login(tmp_path, caplog):
    original, temporary = tmp_path / "auth.json", tmp_path / "temporary.json"
    original.write_bytes(b"old")
    codex._persist_refreshed_auth(original, temporary, b"old")
    assert original.read_bytes() == b"old" and "could not save it" in caplog.text
    temporary.write_bytes(b"old")
    codex._persist_refreshed_auth(original, temporary, b"old")
    assert original.read_bytes() == b"old"


def test_generator_context_reset_ignores_foreign_context_token():
    var = contextvars.ContextVar("test_cov_factory", default="unset")
    token = var.set("value")
    contextvars.Context().run(codex._reset_quietly, var, token)
    assert var.get() == "value"
    var.reset(token)
    assert var.get() == "unset"


@pytest.mark.asyncio
async def test_codex_protocol_refuses_unexpected_early_native_request_and_preserves_cache_usage():
    proc = Process([{"method": "native/request", "id": 99}, *events(
        {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {
            "inputTokens": 12, "outputTokens": 3, "cachedInputTokens": 7,
            "cacheWriteInputTokens": 2, "reasoningOutputTokens": 1}}}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "final", "text": "Final answer"}}},
    )])
    usage = {}
    runtime = CodexRuntime(name="fixture", instructions="", registry={})
    result = "".join([part async for part in runtime._exchange(proc, "hello", "/tmp", usage)])
    assert result == "Final answer"
    assert next(m for m in proc.messages if m.get("id") == 99)["error"]["code"] == -32601
    assert usage["cache_read_tokens"] == 7 and usage["cache_write_tokens"] == 2 and usage["reasoning_tokens"] == 1


@pytest.mark.asyncio
async def test_readonly_hints_are_cached_and_failures_are_conservative(monkeypatch):
    server = SimpleNamespace(list_tools=AsyncMock(return_value=[
        SimpleNamespace(name="read", annotations=SimpleNamespace(readOnlyHint=True)),
        SimpleNamespace(name="write", annotations=SimpleNamespace(readOnlyHint=False)),
        SimpleNamespace(name="unknown", annotations=None)]))

    class Managed:
        async def __aenter__(self): return server
        async def __aexit__(self, *a): return False

    load = Mock(return_value=[Managed()])
    monkeypatch.setattr(factory, "load_mcp_servers", load)
    hint = claude._read_only_hints({"server": {"transport": "sse", "url": "https://fake"}})
    assert await hint("server", "read") is True
    assert await hint("server", "write") is False
    assert await hint("server", "unknown") is None
    assert load.call_count == server.list_tools.await_count == 1
    assert await hint("missing", "read") is None
    monkeypatch.setattr(factory, "load_mcp_servers", lambda specs: [])
    assert await claude._read_only_hints({"empty": {}})("empty", "read") is None
    server.list_tools.side_effect = RuntimeError("unreachable")
    assert await codex._read_only_tools(server) == {}


@pytest.mark.asyncio
async def test_claude_adapter_returns_specific_tool_error_without_crashing():
    tool = SimpleNamespace(name="broken", description="", params_json_schema={},
                           on_invoke_tool=AsyncMock(side_effect=ValueError("invalid argument")))
    wrapped = claude._to_claude_tool(tool)
    assert await wrapped.handler({"key": "value"}) == {"content": [{"type": "text", "text":
        "[tool broken error: ValueError: invalid argument]"}]}


@pytest.mark.asyncio
async def test_claude_external_tool_audit_has_outcome_duration_and_one_record_per_call(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(audit_log, "enabled", lambda: True)
    monkeypatch.setattr(audit_log, "tool_call", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(evaluator, "check_run", lambda *a, **kw: evaluator.ALLOW)
    monkeypatch.setattr(guard, "server_source", lambda *a: ("external", None))
    options = claude._identity_checked(ClaudeAgentOptions(mcp_servers={"external": {}}),
                                      tmp_path, Identity.make("person@example.org"))
    before = options.hooks["PreToolUse"][0].hooks[0]
    after = options.hooks["PostToolUse"][0].hooks[0]
    failed = options.hooks["PostToolUseFailure"][0].hooks[0]
    tool = {"tool_name": "mcp__external__lookup", "tool_input": {"query": "fixture"}}
    assert await before(tool, "success", None) == {}
    assert await after({"tool_response": {"isError": True}}, "success", None) == {}
    assert await after({}, "success", None) == {}
    assert await before(tool, "interrupted", None) == {}
    assert await failed({"is_interrupt": True}, "interrupted", None) == {}
    assert await before(tool, "failure", None) == {}
    assert await failed({}, "failure", None) == {}
    assert [r["outcome"] for r in recorded] == ["error", "error", "error"]
    assert recorded[0]["tool"] == "lookup" and recorded[0]["duration_ms"] >= 0
    assert [r.get("error") for r in recorded] == [None, "interrupted", "tool_error"]
    assert all(r["arguments"] == {"query": "fixture"} for r in recorded)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["mcp__external__lookup", "Bash"])
async def test_claude_post_tool_hook_scrubs_live_app_token(tmp_path, monkeypatch, name):
    monkeypatch.setattr(audit_log, "enabled", lambda: False)
    monkeypatch.setattr(scope, "live_token", lambda: "synthetic-private-token")
    options = claude._identity_checked(ClaudeAgentOptions(), tmp_path, Identity.make("person@example.org"))
    hook = options.hooks["PostToolUse"][0].hooks[0]
    out = await hook({"tool_name": name, "tool_response": {"text": "token synthetic-private-token"}}, "call", None)
    field = "updatedMCPToolOutput" if name.startswith("mcp__") else "updatedToolOutput"
    assert out["hookSpecificOutput"][field] == {"text": "token [redacted]"}
    assert await hook({"tool_name": name, "tool_response": {"text": "public"}}, "call", None) == {}


@pytest.mark.asyncio
async def test_claude_permission_exception_fails_closed_and_audits_refusal(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(evaluator, "check_run", Mock(side_effect=OSError("database down")))
    monkeypatch.setattr(audit_log, "enabled", lambda: True)
    monkeypatch.setattr(audit_log, "tool_call", lambda **kw: recorded.append(kw))
    options = claude._identity_checked(ClaudeAgentOptions(), tmp_path, Identity.make("person@example.org"))
    hook = options.hooks["PreToolUse"][0].hooks[0]
    out = await hook({"tool_name": "untrusted"}, "call", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "This account no longer has access."
    assert recorded[0]["decision"] == "deny" and recorded[0]["tool"] == "untrusted"
