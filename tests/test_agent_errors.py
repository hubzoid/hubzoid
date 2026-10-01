"""A usage limit, a refused login or an overload reads as one plain sentence on
every surface (``hubzoid.agent_errors``), with the reset time in the hub's time
zone, while scheduled tasks, evals and workflows still record the run as failed.

The error texts are the ones the claude CLI and the API return."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from hubzoid import _request_ctx, agent_errors
from hubzoid.run_events import Notice

SESSION = ("ResultError: Claude Code returned an error result: You've hit your session limit "
           "· resets 7:10am (UTC) (exit code: 1)")
WEEKLY = "You've hit your weekly limit · resets Sep 25, 5am (UTC)"
DATED = "You've hit your weekly limit · resets Sep 18, 5am (UTC)"
E429 = ('Claude Code returned an error result: API Error: 429 {"type":"error","error":'
        '{"type":"rate_limit_error","message":"This request would exceed your rate limit."}}')
E401 = ('Claude Code returned an error result: API Error: 401 {"type":"error","error":'
        '{"type":"authentication_error","message":"OAuth token has expired."}} · Please run /login')
E529 = ('Claude Code returned an error result: API Error: 529 {"type":"error","error":'
        '{"type":"overloaded_error","message":"Overloaded"}}')
BARE = ("ProcessError: Command failed with exit code 1 (exit code: 1)\n"
        "Error output: Check stderr output for details")

KOLKATA = ZoneInfo("Asia/Kolkata")
SESSION_SENTENCE = ("The assistant has reached its usage limit for now. It will be available "
                    "again at 12:40 PM (in about 1 hr 20 min). Please ask again after that.")


@pytest.fixture
def clock(monkeypatch):
    """A hub in Asia/Kolkata at 11:20 IST (05:50 UTC) on 26 Sep 2026."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    at = {"now": datetime(2026, 9, 26, 5, 50, tzinfo=timezone.utc)}
    monkeypatch.setattr(agent_errors, "_now", lambda zone: at["now"].astimezone(zone))
    return at


# -- classify ------------------------------------------------------------------------
@pytest.mark.parametrize("text, kind", [
    (SESSION, "usage_limit"),
    (WEEKLY, "usage_limit"),
    (DATED, "usage_limit"),
    ("Claude AI usage limit reached|1790000000", "usage_limit"),
    ("5-hour limit reached ∙ resets 3pm", "usage_limit"),
    (E429, "usage_limit"),
    (E401, "auth"),
    ("Invalid API key · Please run /login", "auth"),
    (E529, "overloaded"),
    ("RuntimeError: boom", "other"),
    (BARE, "other"),
    ("claude run ended with error_max_turns", "other"),
    # Codex's own generic text names usage limits as a thing to check, not a cause.
    ("Codex turn failed or was interrupted. Check login, model availability and usage limits.",
     "other"),
])
def test_classify_real_error_texts(text, kind):
    assert agent_errors.classify(text) == kind


def test_http_status_decides_when_known():
    assert agent_errors.classify("", status=429) == "usage_limit"
    assert agent_errors.classify("", status=401) == "auth"
    assert agent_errors.classify("", status=403) == "auth"
    assert agent_errors.classify("", status=529) == "overloaded"
    assert agent_errors.classify("boom", status=500) == "other"


# -- reset time and the sentence ---------------------------------------------------------
def test_session_reset_in_the_hubs_time_zone(clock):
    reset = agent_errors.reset_time(SESSION)
    assert reset == datetime(2026, 9, 26, 12, 40, tzinfo=KOLKATA)
    assert reset.utcoffset() == KOLKATA.utcoffset(reset)
    assert agent_errors.message("usage_limit", reset) == SESSION_SENTENCE


def test_weekly_reset_names_the_local_date(clock):
    clock["now"] = datetime(2026, 9, 23, 6, 30, tzinfo=timezone.utc)   # 12:00 IST
    reset = agent_errors.reset_time(WEEKLY)
    assert reset == datetime(2026, 9, 25, 10, 30, tzinfo=KOLKATA)
    assert agent_errors.message("usage_limit", reset) == (
        "The assistant has reached its usage limit for now. It will be available again on "
        "Sep 25 at 10:30 AM (in about 1 day 22 hr). Please ask again after that.")


def test_time_only_reset_already_past_today_is_tomorrow(clock):
    clock["now"] = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)   # 03:30 IST on the 27th
    reset = agent_errors.reset_time("You've hit your session limit · resets 2am (UTC)")
    assert reset == datetime(2026, 9, 27, 7, 30, tzinfo=KOLKATA)
    assert "at 7:30 AM (in about 4 hr)" in agent_errors.message("usage_limit", reset)


def test_epoch_reset_from_older_clis(clock):
    epoch = int(datetime(2026, 9, 26, 7, 10, tzinfo=timezone.utc).timestamp())
    reset = agent_errors.reset_time(f"Claude AI usage limit reached|{epoch}")
    assert reset == datetime(2026, 9, 26, 12, 40, tzinfo=KOLKATA)


def test_without_a_time_the_sentence_says_a_few_hours(clock):
    assert agent_errors.reset_time(E429) is None
    assert agent_errors.reset_time("resets 7:10am (Nowhere/Unknown)") is None
    assert agent_errors.message("usage_limit") == (
        "The assistant has reached its usage limit for now. It will be available again in a "
        "few hours. Please ask again after that.")
    # A reset long past (a stale date) is not shown as a time.
    stale = agent_errors.reset_time(DATED)
    assert "in a few hours" in agent_errors.message("usage_limit", stale)


def test_hub_zone_is_tz_else_the_servers(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    assert agent_errors.hub_zone() == KOLKATA
    for value in ("not a zone", None):
        if value is None:
            monkeypatch.delenv("TZ")
        else:
            monkeypatch.setenv("TZ", value)
        zone = agent_errors.hub_zone()
        assert not isinstance(zone, ZoneInfo)        # the server's own local zone
        assert zone.utcoffset(None) == datetime.now().astimezone().utcoffset()


def test_sentences_hold_no_vendor_or_account_words():
    texts = [*agent_errors.MESSAGES.values(), SESSION_SENTENCE]
    for text in texts:
        for word in ("claude", "token", "subscription", "anthropic", "traceback", "error"):
            assert word not in text.lower(), (word, text)


# -- notice -------------------------------------------------------------------------------
def test_notice_plain_for_a_known_class_and_unchanged_for_other(clock):
    with _request_ctx.chat_scope(None):
        n = agent_errors.notice(E529)
        assert n == Notice(kind="error", text=agent_errors.MESSAGES["overloaded"],
                           legacy="\n\n" + agent_errors.MESSAGES["overloaded"],
                           error_kind="overloaded")
        assert _request_ctx.run_failure() == {"kind": "overloaded", "reset_at": None}
    with _request_ctx.chat_scope(None):
        n = agent_errors.notice("RuntimeError: boom")
        assert n == Notice(kind="error", text="RuntimeError: boom",
                           legacy="\n\n[agent error: RuntimeError: boom]")
        assert _request_ctx.run_failure()["kind"] == "other"


def test_stderr_decides_only_when_the_error_says_nothing(clock):
    assert agent_errors.describe(BARE, stderr=WEEKLY).kind == "usage_limit"
    assert agent_errors.describe("RuntimeError: boom", stderr="").kind == "other"
    # The error text wins over stderr noise.
    assert agent_errors.describe(E529, stderr="HTTP 401 from an MCP server").kind == "overloaded"


def test_failure_is_recorded_only_inside_a_scope():
    agent_errors.notice(E529)                     # no scope: nothing to record into
    assert _request_ctx.run_failure() is None
    with _request_ctx.chat_scope(None):
        assert _request_ctx.run_failure() is None  # a fresh scope starts clean


# -- the Claude runtime: both error paths ------------------------------------------------------
def _claude_runtime(monkeypatch, fake_query):
    import claude_agent_sdk
    from claude_agent_sdk import ClaudeAgentOptions

    from hubzoid.factory_claude import ClaudeRuntime

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query, raising=False)
    return ClaudeRuntime(name="t", options=ClaudeAgentOptions(), thinking_mode="off",
                         tool_mode="off", hub_dir=None)


def _turn(rt, prompt="hi"):
    async def go():
        with _request_ctx.chat_scope(None):
            items = [i async for i in rt.stream_events(prompt)]
            return items, _request_ctx.run_failure()

    return asyncio.run(go())


def test_claude_exception_path_shows_the_plain_sentence(monkeypatch, clock):
    from claude_agent_sdk import ResultError

    async def fake_query(*, prompt, options):
        raise ResultError(
            "Claude Code returned an error result: You've hit your session limit · resets 7:10am (UTC)",
            data={"subtype": "success", "is_error": True}, exit_code=1)
        yield  # pragma: no cover

    rt = _claude_runtime(monkeypatch, fake_query)
    items, failure = _turn(rt)
    assert items[-1] == Notice(kind="error", text=SESSION_SENTENCE, legacy="\n\n" + SESSION_SENTENCE,
                               error_kind="usage_limit")
    assert failure == {"kind": "usage_limit", "reset_at": "2026-09-26T12:40:00+05:30"}
    assert rt.last_error is not None                # run_once still raises


def test_claude_error_result_path_shows_the_plain_sentence(monkeypatch, clock):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options):
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True,
                            num_turns=1, session_id="s", result="API Error: Repeated server errors",
                            api_error_status=529)

    rt = _claude_runtime(monkeypatch, fake_query)
    items, failure = _turn(rt)
    assert items[-1].text == agent_errors.MESSAGES["overloaded"]
    assert items[-1].error_kind == "overloaded" and failure["kind"] == "overloaded"
    text = asyncio.run(rt.run("hi"))
    assert text == "\n\n" + agent_errors.MESSAGES["overloaded"] and "[agent error" not in text


def test_claude_bare_exit_code_is_classified_from_the_clis_stderr(monkeypatch, clock):
    from claude_agent_sdk import ProcessError

    clock["now"] = datetime(2026, 9, 23, 6, 30, tzinfo=timezone.utc)

    async def fake_query(*, prompt, options):
        options.stderr("Error: You've hit your weekly limit · resets Sep 25, 5am (UTC)\n")
        raise ProcessError("Command failed with exit code 1", exit_code=1,
                           stderr="Check stderr output for details")
        yield  # pragma: no cover

    rt = _claude_runtime(monkeypatch, fake_query)
    items, failure = _turn(rt)
    assert items[-1].error_kind == "usage_limit"
    assert "on Sep 25 at 10:30 AM" in items[-1].text
    assert failure["reset_at"] == "2026-09-25T10:30:00+05:30"


def test_claude_other_errors_keep_the_marker(monkeypatch, clock):
    async def fake_query(*, prompt, options):
        raise RuntimeError("CLI exited with code 1")
        yield  # pragma: no cover

    rt = _claude_runtime(monkeypatch, fake_query)
    items, failure = _turn(rt)
    assert items[-1] == Notice(kind="error", text="RuntimeError: CLI exited with code 1",
                               legacy="\n\n[agent error: RuntimeError: CLI exited with code 1]")
    assert failure["kind"] == "other"


# -- Codex: the turn's structured error class ----------------------------------------------------
def test_codex_usage_limit_turn_is_a_plain_sentence(monkeypatch, tmp_path, clock):
    from tests import run_event_scenarios as scenarios

    failed = {"method": "turn/completed", "params": {"threadId": "thread", "turn": {
        "status": "failed", "error": {"message": "You've hit your usage limit.",
                                      "codexErrorInfo": "usageLimitExceeded"}}}}
    monkeypatch.setitem(scenarios.CODEX_SCENARIOS, "usage_limit", [scenarios._delta("Part"), failed])
    items = scenarios.run_codex(monkeypatch, tmp_path, "usage_limit", tool_mode="off", typed=True)
    assert items[-1].error_kind == "usage_limit"
    assert items[-1].text == agent_errors.MESSAGES["usage_limit"]
    # A failed turn with no class keeps Codex's own notice.
    items = scenarios.run_codex(monkeypatch, tmp_path, "turn_failed", tool_mode="off", typed=True)
    assert items[-1].error_kind == "" and items[-1].text.startswith("Codex could not complete")


# -- surfaces --------------------------------------------------------------------------------------
@pytest.fixture
def chat_app(tmp_path, monkeypatch, clock):
    from tests.chat_helpers import build_app, make_hub

    make_hub(tmp_path, monkeypatch)
    app = build_app()

    async def limited(prompt):  # noqa: ARG001
        yield "Half"
        yield agent_errors.notice(SESSION)

    monkeypatch.setattr(app.state.chat.runtime, "stream_events", limited)
    return app


def test_web_app_stream_and_stored_reply_carry_the_class(chat_app):
    from fastapi.testclient import TestClient

    from tests.chat_helpers import ORIGIN, chat_body, events

    client = TestClient(chat_app)
    r = client.post("/api/chat", headers=ORIGIN, json=chat_body(
        "c_limit00001", "hi", agent=chat_app.state.chat.model_label,
        message_id="m_user00001", assistant_id="m_asst00001"))
    evs = events(r.text)
    assert {"type": "error", "errorText": SESSION_SENTENCE} in evs
    assert evs[-2] == {"type": "finish",
                       "messageMetadata": {"status": "error", "errorKind": "usage_limit"}}
    assert "session limit" not in r.text and "agent error" not in r.text
    reply = client.get("/api/runs/m_asst00001").json()["message"]
    assert reply["error"] == SESSION_SENTENCE and reply["error_kind"] == "usage_limit"
    stored = client.get("/api/conversations/c_limit00001", headers=ORIGIN).json()["messages"]
    assert stored[1]["error_kind"] == "usage_limit" and "error_kind" not in stored[0]


def test_openai_compatible_output_shows_the_plain_sentence(chat_app):
    from fastapi.testclient import TestClient

    client = TestClient(chat_app)
    auth = {"Authorization": "Bearer k-chat-test"}
    body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
    content = client.post("/v1/chat/completions", headers=auth, json=body).json()[
        "choices"][0]["message"]["content"]
    assert content == "Half\n\n" + SESSION_SENTENCE
    streamed = client.post("/v1/chat/completions", headers=auth, json={**body, "stream": True}).text
    assert "12:40 PM" in streamed and "agent error" not in streamed and "session limit" not in streamed


# -- failure detection: schedules, evals, workflows ------------------------------------------------------
class _LimitedRuntime:
    """A runtime whose every turn hits the session limit, as ClaudeRuntime reports it."""

    name = "limited"
    last_error = None

    async def aopen(self):
        pass

    async def aclose(self):
        pass

    async def stream_events(self, prompt):  # noqa: ARG002
        self.last_error = RuntimeError("Claude Code returned an error result: session limit")
        yield agent_errors.notice(SESSION)

    async def run(self, prompt):
        from hubzoid import run_events

        return "".join([t async for t in run_events.as_text(self.stream_events(prompt))])


def test_scheduled_task_at_a_usage_limit_is_recorded_as_failed(tmp_path, clock):
    from hubzoid import schedule_runner as runner
    from hubzoid import scheduling as sch

    task = sch.ScheduledTask(name="limited", schedule="0 3 * * *", cron=sch.parse_cron("0 3 * * *"),
                             body="do the thing", max_rounds=10)
    res = asyncio.run(runner.run_task(tmp_path, task, runtime_factory=lambda *a: _LimitedRuntime()))
    assert res.result == "error" and res.rounds == 3          # stopped like any backend error
    assert "usage_limit" in res.error and "2026-09-26T12:40:00+05:30" in res.error
    assert sch.ScheduleState(tmp_path).get("limited")["last_result"] == "error"


def test_eval_case_at_a_usage_limit_is_an_error(clock):
    from hubzoid.evals import runner
    from hubzoid.evals.cases import EvalCase

    case = EvalCase(name="ping", prompt="ping", contains=["pong"])
    result = asyncio.run(runner._run_one(_LimitedRuntime(), case))
    assert result.error.startswith("agent error (usage_limit): The assistant has reached")
    assert result.checks == []


def test_workflow_agent_call_at_a_usage_limit_fails_with_the_class(tmp_path, monkeypatch, clock):
    from hubzoid import runtime as runtime_lib

    monkeypatch.setattr(runtime_lib, "build", lambda hub_dir, **kw: _LimitedRuntime())
    with pytest.raises(runtime_lib.AgentRunError, match=r"agent run failed \(usage_limit\)"):
        runtime_lib.run_once(tmp_path, "send the weekly email")
