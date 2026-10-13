from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import claude_agent_sdk as sdk
import pytest
from sqlalchemy import text

from hubzoid import _request_ctx, claude_credentials as creds, config_secrets
from hubzoid.factory_claude import ClaudeRuntime, _query_with_failover, claude_complete


@pytest.fixture
def configured(tmp_path, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in zip(creds.TOKEN_KEYS, ("fake-primary", "fake-secondary", "fake-tertiary")):
        monkeypatch.setenv(key, value)
    return tmp_path


def result(error=False):
    return sdk.ResultMessage(subtype="error_during_execution" if error else "success",
                             duration_ms=1, duration_api_ms=1, is_error=error,
                             num_turns=1, session_id="s", result=None if error else "done")


def failure(kind="rate_limit", detail="Limit reached"):
    return sdk.AssistantMessage(content=[sdk.TextBlock(text=detail)], model="sonnet", error=kind)


def runtime(hub):
    opts = sdk.ClaudeAgentOptions(model="sonnet", tools=[], env={"KEEP": "yes"})
    rt = ClaudeRuntime(name="t", options=opts, hub_dir=hub)
    rt._options_for_turn = lambda: opts
    return rt


def test_two_backups_then_sticky_selection_and_no_intermediate_error(configured, monkeypatch):
    calls, closed = [], []
    async def query(*, prompt, options):
        token = options.env[creds.TOKEN_KEYS[0]]
        assert all(options.env[k] == "" for k in creds.TOKEN_KEYS[1:])
        assert options.model == "sonnet" and options.tools == [] and options.env["KEEP"] == "yes"
        calls.append(token)
        try:
            if token != "fake-tertiary":
                yield failure()
                yield result(True)
            else:
                assert closed[-2:] == ["fake-primary", "fake-secondary"] or len(calls) > 3
                yield result()
        finally:
            closed.append(token)
    monkeypatch.setattr(sdk, "query", query)
    rt = runtime(configured)
    with _request_ctx.chat_scope("chat"):
        assert asyncio.run(rt.run("hello")) == "done"
        assert _request_ctx.run_failure() is None and rt.last_error is None
    assert asyncio.run(runtime(configured).run("next turn")) == "done"
    assert calls == ["fake-primary", "fake-secondary", "fake-tertiary", "fake-tertiary"]


@pytest.mark.parametrize("error", ["invalid_request", "server_error", "billing_error", "unknown"])
def test_unrelated_errors_never_rotate(configured, monkeypatch, error):
    calls = []
    async def query(**kwargs):
        calls.append(1)
        yield failure(error, "MCP 401: rate limit 429")
        yield result(True)
    monkeypatch.setattr(sdk, "query", query)
    rt = runtime(configured)
    asyncio.run(rt.run("hello"))
    assert len(calls) == 1 and rt.last_error is not None
    assert creds.Pool.configured(configured)._read(creds.Credential(0, "fake-primary").key) is None


@pytest.mark.parametrize("activity", ["text", "thinking", "hidden_tool"])
def test_no_replay_after_progress_but_next_request_uses_backup(configured, monkeypatch, activity):
    calls = []
    async def query(*, prompt, options):
        token = options.env[creds.TOKEN_KEYS[0]]
        calls.append(token)
        if token != "fake-primary":
            yield result()
            return
        if activity == "hidden_tool":
            await options.hooks["PreToolUse"][0].hooks[0]({}, "tool", {})
        else:
            delta = {"type": "text_delta", "text": "partial"} if activity == "text" else {
                "type": "thinking_delta", "thinking": "work"}
            yield sdk.types.StreamEvent(uuid="e", session_id="s", event={
                "type": "content_block_delta", "delta": delta})
        yield failure()
        yield result(True)
    monkeypatch.setattr(sdk, "query", query)
    rt = runtime(configured)
    asyncio.run(rt.run("hello"))
    assert calls == ["fake-primary"] and rt.last_error is not None
    assert asyncio.run(runtime(configured).run("independent request")) == "done"
    assert calls == ["fake-primary", "fake-secondary"]


def test_exhaustion_and_no_queries_during_shared_cooldown(configured, monkeypatch):
    calls = []
    async def query(**kwargs):
        calls.append(1)
        yield failure()
        yield result(True)
    monkeypatch.setattr(sdk, "query", query)
    assert "usage limit" in asyncio.run(runtime(configured).run("hi"))
    assert "usage limit" in asyncio.run(runtime(configured).run("again"))
    assert len(calls) == 3


def test_shared_reset_probe_and_late_success_does_not_erase_failure(configured, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(creds.time, "time", lambda: now[0])
    pool = creds.Pool.configured(configured)
    primary = pool.credentials[0]
    pool.failed(primary, creds.Unavailable(reset_at=2000))
    other = creds.Pool.configured(configured)
    assert other.select(set())[0].slot == 1
    now[0] = 2001
    with ThreadPoolExecutor(max_workers=2) as executor:
        selections = list(executor.map(lambda _: other.select(set()), range(2)))
    assert sorted(c.slot for c, _ in selections) == [0, 1]
    claim = next(raw for c, raw in selections if c.slot == 0)
    pool.failed(primary, creds.Unavailable(reset_at=3000))
    other.recovered(primary, claim)
    assert pool.select(set())[0].slot == 1
    now[0] = 3001
    credential, claim = other.select(set())
    other.recovered(credential, claim)
    assert pool.select(set()) == (primary, None)
    with pool.engine.connect() as conn:
        assert "fake-" not in str(conn.execute(text("SELECT k,v FROM hz_meta")).all())


def test_auth_stays_disabled_until_token_is_replaced(configured, monkeypatch):
    pool = creds.Pool.configured(configured)
    pool.failed(pool.credentials[0], creds.Unavailable("auth"))
    assert creds.Pool.configured(configured).select(set())[0].slot == 1
    monkeypatch.setenv(creds.TOKEN_KEYS[0], "fake-replacement")
    assert creds.Pool.configured(configured).select(set())[0].slot == 0


def test_configuration_and_secret_masking(configured, monkeypatch):
    monkeypatch.setenv(creds.TOKEN_KEYS[2], "fake-primary")
    assert len(creds.Pool.configured(configured).credentials) == 2
    assert config_secrets.child_env_overrides({k: "x" for k in creds.TOKEN_KEYS}) == {
        k: "" for k in creds.TOKEN_KEYS[1:]}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    with pytest.raises(ValueError, match="cannot be combined"):
        creds.Pool.configured(configured)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv(creds.TOKEN_KEYS[0])
    with pytest.raises(ValueError, match="require CLAUDE"):
        creds.Pool.configured(configured)
    for key in creds.TOKEN_KEYS[1:]:
        monkeypatch.delenv(key)
    assert creds.Pool.configured(None) is None


def test_tool_free_calls_recover_and_cancellation_closes_query(configured, monkeypatch):
    calls = []
    async def query(*, prompt, options):
        token = options.env[creds.TOKEN_KEYS[0]]
        calls.append(token)
        if token == "fake-primary":
            yield failure()
            yield result(True)
        else:
            yield result()
    monkeypatch.setattr(sdk, "query", query)
    answer, _ = asyncio.run(claude_complete("hi", hub_dir=configured))
    assert answer == "done" and calls == ["fake-primary", "fake-secondary"]
    closed = []
    async def cancelled(**kwargs):
        try:
            raise asyncio.CancelledError()
            yield
        finally:
            closed.append(True)
    monkeypatch.setattr(sdk, "query", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(claude_complete("hi", hub_dir=configured))
    assert closed == [True]


def test_unknown_exception_redacted_without_rotation(configured, monkeypatch, caplog):
    calls = []
    async def query(**kwargs):
        calls.append(1)
        raise RuntimeError("tool 429 failed fake-primary fake-secondary")
        yield
    monkeypatch.setattr(sdk, "query", query)
    answer = asyncio.run(runtime(configured).run("hi"))
    assert len(calls) == 1
    assert "fake-primary" not in answer + caplog.text
    assert "fake-secondary" not in answer + caplog.text


def test_provider_reset_is_shared_across_hubs_and_warning_does_not_rotate(configured, monkeypatch):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{configured / 'shared.db'}")
    calls = []
    async def query(*, prompt, options):
        token = options.env[creds.TOKEN_KEYS[0]]
        calls.append(token)
        yield sdk.types.RateLimitEvent(uuid="r", session_id="s", rate_limit_info=sdk.types.RateLimitInfo(
            status="rejected" if token == "fake-primary" else "allowed_warning", resets_at=4102444800))
        if token == "fake-primary":
            yield failure()
            failed = result(True)
            failed.usage = {"input_tokens": 2}
            yield failed
        else:
            done = result()
            done.usage = {"input_tokens": 3}
            yield done
    monkeypatch.setattr(sdk, "query", query)
    with _request_ctx.chat_scope("chat"):
        assert asyncio.run(runtime(configured).run("hello")) == "done"
        assert _request_ctx.drain_usage()["input_tokens"] == 5
    other_hub = configured / "other"
    other_hub.mkdir()
    other = creds.Pool.configured(other_hub)
    assert other.select(set())[0].slot == 1
    assert other.unavailable().reset_at == 4102444800
    assert asyncio.run(runtime(other_hub).run("hello")) == "done"
    assert calls == ["fake-primary", "fake-secondary", "fake-secondary"]


def test_async_image_prompt_is_recreated_for_each_attempt(configured, monkeypatch):
    received = []
    def prompt():
        async def image():
            yield {"image": "same image"}
        return image()
    async def query(*, prompt, options):
        received.append([part async for part in prompt])
        if len(received) == 1:
            yield failure("authentication_failed")
            yield result(True)
        else:
            yield result()
    monkeypatch.setattr(sdk, "query", query)
    async def run():
        return [m async for m in _query_with_failover(prompt=prompt,
            options=sdk.ClaudeAgentOptions(), hub_dir=configured)]
    assert len(asyncio.run(run())) == 1
    assert received == [[{"image": "same image"}], [{"image": "same image"}]]


@pytest.mark.slow
def test_postgres_shared_recovery_claim(configured, monkeypatch, postgres_url):
    from sqlalchemy import create_engine
    from hubzoid import migrations
    engine = create_engine(postgres_url)
    migrations.upgrade(engine, "operational")
    pool = creds.Pool(engine, [creds.Credential(0, "postgres-primary"), creds.Credential(1, "postgres-backup")])
    clock = [1000]
    monkeypatch.setattr(creds.time, "time", lambda: clock[0])
    pool.failed(pool.credentials[0], creds.Unavailable(reset_at=1100))
    clock[0] = 1101
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            selections = list(executor.map(lambda _: pool.select(set()), range(2)))
        assert sorted(c.slot for c, _ in selections) == [0, 1]
    finally:
        engine.dispose()
