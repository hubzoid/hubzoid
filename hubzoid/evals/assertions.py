"""Model-free checks — assertions over an already-finished run.

"Free" here means these checks add no model call of their own. It does NOT mean
running a case is free: every case always invokes the hub's agent, which is a
real, paid model call. `--no-judge` skips the extra grading call, so it is
cheaper, not free.

These run first, on every case, always. The judge (`judge.py`) only runs when
they pass: there is no point paying for a second model call to grade an answer
that already failed on a missing keyword or a forbidden tool call.

Four kinds of check:

  * `contains` / `not_contains` — substring, case-insensitive (see below)
  * `expect_tools` / `forbid_tools` — did the agent actually call it
  * `expect_tool_args`            — did it call it with the right arguments
  * `timeout`                     — hard bound, exceeded = fail
  * (errors)                      — the run itself blew up

Everything here is a pure function over an already-finished run, so the whole
module is testable without a model, a network, or a hub.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..tool_events import short_name
from . import calls as calls_lib


@dataclass
class Check:
    """One assertion's verdict. `detail` is what the terminal table shows."""
    kind: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "passed": self.passed, "detail": self.detail}


# --------------------------------------------------------------------------
# Display chrome
# --------------------------------------------------------------------------
# `Runtime.run()` returns the same string the chat surface renders, which
# includes tool-activity lines and (on the Claude backend) a `<think>` panel.
# Those are display artifacts, not the agent's answer, and asserting against
# them gives both false passes (`contains: ["read_knowledge"]` matching the
# tool line rather than the prose) and false failures (`not_contains` tripping
# over the agent's own reasoning). So we assert against the answer only.
#
# The patterns below mirror `tool_events.format_call` / `format_error` /
# `format_tool_block` and the
# Claude `_ThinkWriter`. `tests/test_evals.py` feeds real output from those
# functions through `strip_chrome`, so a format change breaks a test rather
# than silently corrupting eval verdicts.

_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)
_DETAILS_RE = re.compile(r"<details>\s*<summary>\s*[✓↳⚠].*?(?:</details>|\Z)",
                         re.DOTALL | re.IGNORECASE)
# `tool_events.format_tool_block`: the chat app's tool-call block.
_TOOL_BLOCK_RE = re.compile(r'<details\s+type="tool_calls"[^>]*>.*?(?:</details>|\Z)',
                            re.DOTALL | re.IGNORECASE)
_TOOL_LINE_RE = re.compile(r"^[ \t]*>[ \t]*[✓↳⚠].*$", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def strip_chrome(text: str) -> str:
    """Return just the agent's answer: no thinking panel, no tool lines.

    Order matters — the `<details>` tool dropdown is removed before the
    blockquote form, and both before blank-line collapsing.
    """
    out = _THINK_RE.sub("", text or "")
    out = _TOOL_BLOCK_RE.sub("", out)
    out = _DETAILS_RE.sub("", out)
    out = _TOOL_LINE_RE.sub("", out)
    return _BLANK_RUN_RE.sub("\n\n", out).strip()


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------
def _norm_tool(name: str) -> str:
    """Canonical tool name for comparison.

    Both backends prefix tool names (`mcp__hubzoid__read_knowledge`), and a
    hub author writes the short name. `short_name` strips the prefix; casing
    is normalised so `read_knowledge` and `Read_Knowledge` are the same tool.
    """
    return short_name((name or "").strip()).lower()


def check_contains(response: str, needles: list[str]) -> list[Check]:
    """Substring checks, **case-insensitive**.

    Case-insensitive is the friendlier default and almost never the wrong one:
    a hub author writing `contains: ["14 days"]` means the fact, not the
    capitalisation, and `not_contains: ["ERROR"]` should catch `error` too. A
    case-sensitive check would turn cosmetic drift into red builds.
    """
    hay = response.lower()
    return [
        Check("contains", n.lower() in hay, "" if n.lower() in hay else f'missing: "{n}"')
        for n in needles
    ]


def check_not_contains(response: str, needles: list[str]) -> list[Check]:
    hay = response.lower()
    out: list[Check] = []
    for n in needles:
        hit = n.lower() in hay
        out.append(Check("not_contains", not hit, f'present: "{n}"' if hit else ""))
    return out


def check_expect_tools(tool_calls: list[str], expected: list[str]) -> list[Check]:
    called = {_norm_tool(t) for t in tool_calls}
    out: list[Check] = []
    for want in expected:
        ok = _norm_tool(want) in called
        out.append(Check("expect_tools", ok, "" if ok else f"never called: {want}"))
    return out


def check_forbid_tools(tool_calls: list[str], forbidden: list[str],
                       tools: list | None = None) -> list[Check]:
    """`tools` (call records with arguments), when given, puts the offending
    call's key arguments in the failure reason."""
    called = {_norm_tool(t) for t in tool_calls}
    out: list[Check] = []
    for banned in forbidden:
        hit = _norm_tool(banned) in called
        detail = ""
        if hit:
            detail = f"forbidden tool called: {banned}"
            call = next((c for c in tools or [] if _norm_tool(c.name) == _norm_tool(banned)), None)
            shown = calls_lib.key_args(call.args) if call is not None else ""
            if shown:
                detail += f" ({shown})"
        out.append(Check("forbid_tools", not hit, detail))
    return out


def check_expect_tool_args(tools: list, expected: dict) -> list[Check]:
    """One check per tool in `expected`: at least one call of that tool has
    every listed argument containing the expected value (case-insensitive
    substring for text, equality otherwise). `tools` are call records."""
    out: list[Check] = []
    for want, args in expected.items():
        mine = [c for c in tools if _norm_tool(c.name) == _norm_tool(want)]
        ok = any(calls_lib.args_match(c.raw_args if c.raw_args is not None else c.args, args)
                 for c in mine)
        detail = ""
        if not ok:
            detail = f"no {want} call with {calls_lib.expected_text(args)}"
            if mine:
                seen = "; ".join(calls_lib.key_args(c.args) or "(no arguments)" for c in mine[:3])
                detail += f" (called with: {seen}{'; …' if len(mine) > 3 else ''})"
            else:
                detail += f" (never called: {want})"
        out.append(Check("expect_tool_args", ok, detail))
    return out


def run_free_checks(
    case,
    *,
    response: str,
    tool_calls: list[str],
    tools: list | None = None,
) -> list[Check]:
    """Every model-free assertion declared on `case`, in table order.

    `response` must already be `strip_chrome`-ed — the runner does that once
    and reuses the clean text for the judge too. `tools` are the call records
    (with arguments) behind `tool_calls`; when absent they are built from the
    names, with no arguments. Tool checks look at every call of the case
    (every turn of a conversation); text checks look at the final reply.
    """
    if tools is None:
        from .results import ToolCallRecord

        tools = [ToolCallRecord(name=n) for n in tool_calls]
    checks: list[Check] = []
    checks += check_expect_tools(tool_calls, case.expect_tools)
    checks += check_forbid_tools(tool_calls, case.forbid_tools, tools)
    checks += check_expect_tool_args(tools, getattr(case, "expect_tool_args", None) or {})
    checks += check_contains(response, case.contains)
    checks += check_not_contains(response, case.not_contains)
    return checks


def first_failure(checks: list[Check]) -> str:
    """The one-line reason shown in the terminal table. '' when all passed."""
    for c in checks:
        if not c.passed:
            return c.detail or c.kind
    return ""
