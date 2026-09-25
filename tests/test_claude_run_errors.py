"""A Claude run the SDK reports as failed (ResultMessage.is_error) surfaces the
same '[agent error: ...]' marker as the OpenAI backend, and records last_error
so run_once raises. A successful run records nothing."""
from __future__ import annotations

import anyio
import claude_agent_sdk
from claude_agent_sdk import ResultMessage

from hubzoid.factory_claude import ClaudeRuntime


class _FakeOptions:
    system_prompt = "sys"


def _result(*, is_error: bool, subtype: str, result: str | None):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1,
                         is_error=is_error, num_turns=1, session_id="s", result=result)


def _run(monkeypatch, message):
    async def _fake_query(*, prompt, options):
        yield message

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query, raising=False)
    rt = ClaudeRuntime(name="t", options=_FakeOptions(), hub_dir=None)
    rt._options_for_turn = lambda: _FakeOptions()
    text = anyio.run(rt.run, "hi")
    return rt, text


def test_sdk_reported_failure_shows_marker_and_sets_last_error(monkeypatch):
    rt, text = _run(monkeypatch, _result(is_error=True, subtype="error_max_turns", result=None))
    assert "[agent error: claude run ended with error_max_turns]" in text
    assert rt.last_error is not None


def test_success_result_is_not_an_error(monkeypatch):
    rt, text = _run(monkeypatch, _result(is_error=False, subtype="success", result="done"))
    assert text == "done"
    assert rt.last_error is None
