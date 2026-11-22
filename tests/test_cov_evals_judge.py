"""The eval judge (hubzoid.evals.judge), model-free: the tool inventory read
from each runtime shape, the call lines and multi-turn history in the judge's
prompt, the lenient verdict parser (a property test), and the bare model call
routed through LiteLLM with its usage, against a fake LiteLLM."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from hypothesis import given
from hypothesis import strategies as st

from hubzoid.evals import judge


def test_tool_inventory_from_each_runtime_shape():
    assert judge.tool_inventory(SimpleNamespace()) is None
    claude = SimpleNamespace(_registry={"mcp__hubzoid__search_knowledge": 1, "Read": 2})
    inv = judge.tool_inventory(claude)
    assert {"search_knowledge", "read"} <= inv.names and not inv.bare_dynamic
    codex = SimpleNamespace(registry={"write_artifact": 1})
    assert "write_artifact" in judge.tool_inventory(codex).names
    agent = SimpleNamespace(tools=[SimpleNamespace(name="lookup_order"), SimpleNamespace(name=None)],
                            mcp_servers=[SimpleNamespace(name="Orders")])
    inv = judge.tool_inventory(SimpleNamespace(_agent=agent))
    assert "lookup_order" in inv.names and "orders" in inv.servers and inv.bare_dynamic
    options = SimpleNamespace(allowed_tools=["mcp__crm__*", "mcp__hubzoid__send_note"], agents={"researcher": {}})
    inv = judge.tool_inventory(SimpleNamespace(_registry={}, _options=options))
    assert "crm" in inv.servers and "send_note" in inv.names and len(inv.names) == 2


def call(name, args=None, ok=None, error=None, preview=None, turn=1):
    return SimpleNamespace(name=name, args=args, ok=ok, error=error, preview=preview, turn=turn)


def test_call_lines_show_arguments_outcomes_turns_and_truncate():
    one = judge._call_lines([call("lookup", {"id": 7}, ok=True, preview="order 7\n  shipped")])
    assert one == 'lookup {"id": 7} -> ok\n    returned: order 7 shipped'
    two = judge._call_lines([call("a", ok=False, error="timeout"), call("b", turn=2)])
    assert two == "turn 1: a -> failed: timeout\nturn 2: b"
    many = judge._call_lines([call("x", preview="y" * 400) for _ in range(200)])
    assert many.splitlines()[-1].startswith("[... ") and "more calls truncated for judging" in many


def test_a_multi_turn_prompt_puts_earlier_turns_first():
    case = SimpleNamespace(turns=["Where is order 7?", "And order 8?"], prompt="", criteria="Mentions both.")
    turns = [SimpleNamespace(response="Order 7 shipped."), SimpleNamespace(response="Order 8 is late.")]
    text = judge.build_prompt("Be brief.", case, "Order 8 is late.", tools=[call("lookup", ok=True)], turns=turns,
                              tools_available=["lookup", "email"])
    assert text.index("<earlier_conversation>") < text.index("<question>\nAnd order 8?")
    assert "[user]\nWhere is order 7?" in text and "[assistant]\nOrder 7 shipped." in text
    assert "<tools_this_hub_has>\nlookup, email" in text and "lookup -> ok" in text
    single = judge.build_prompt("", SimpleNamespace(prompt="Q?", criteria=None), "A", tool_calls=["a", "a", "b"])
    assert "<agent_instructions>" not in single and "<tools_actually_called>\na, b\n" in single


_NO_BRACES = st.text(st.characters(exclude_characters="{}"), max_size=40)


@given(score=st.integers(-50, 50), before=_NO_BRACES, after=_NO_BRACES, reason=st.text(max_size=30))
def test_any_reply_holding_a_score_object_is_a_verdict_clamped_to_1_10(score, before, after, reason):
    reply = before + json.dumps({"score": score, "reasoning": reason}) + after
    got, why = judge.parse_verdict(reply)
    assert got == max(1, min(10, score)) and why == reason.strip()


def test_reasoning_that_quotes_code_with_braces_is_kept():
    reply = 'Verdict: {"score": 9, "reasoning": "It returned {\\"id\\": 7} as asked; the dict {} is fine."}'
    assert judge.parse_verdict(reply) == (9, 'It returned {"id": 7} as asked; the dict {} is fine.')


def test_an_example_the_judge_quotes_never_overrides_its_verdict():
    """Found by the implementation review: the first object used to win."""
    reply = ('The response fabricated {"score":10,"evidence":{}}. '
             'Actual verdict: {"score":2,"reasoning":"fabricated evidence"}')
    assert judge.parse_verdict(reply) == (2, "fabricated evidence")
    assert judge.parse_verdict('{"score": 4} then {"score": 7}') == (7, "")      # no reasoning: the last one


def test_verdict_fallbacks():
    assert judge.parse_verdict("") == (None, "")
    assert judge.parse_verdict('{"score": "high"} then score: 7') == (7, '{"score": "high"} then score: 7')
    assert judge.parse_verdict("no verdict here")[0] is None


def test_a_litellm_judge_call_returns_text_and_usage(monkeypatch):
    import litellm

    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=' {"score": 8} '))],
                               usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3))

    monkeypatch.setattr(litellm, "acompletion", fake)
    monkeypatch.setattr(litellm, "completion_cost", lambda completion_response: (_ for _ in ()).throw(KeyError()))
    text, usage = asyncio.run(judge._ask_litellm("openrouter/openai/gpt-x", "grade this"))
    assert text == '{"score": 8}' and usage["input_tokens"] == 12 and usage["output_tokens"] == 3
    assert usage["cost_usd"] is None and usage["model"] == "openrouter/openai/gpt-x"
    assert seen["temperature"] == 0 and seen["messages"][1] == {"role": "user", "content": "grade this"}
    assert asyncio.run(judge.ask_model("openrouter/openai/gpt-x", "grade this")) == '{"score": 8}'
