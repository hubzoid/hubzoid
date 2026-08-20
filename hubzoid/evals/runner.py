"""Execute eval cases against the hub's own runtime.

The whole point of this module is that it does **not** have a special
execution path. A case runs through `runtime.build(hub)` — the same call the
FastAPI bridge and `hubzoid test` make — so it sees the same model, the same
tools, the same MCP servers, the same skills, the same access guard. Anything
else would be testing a simulation of the hub instead of the hub.

Ordering within a case:

    run the agent  ->  free checks  ->  judge (only if the free checks passed)

The judge is skipped on a case that already failed, because paying a model to
grade an answer we know is wrong buys nothing.

Cases run sequentially, sharing one built runtime. Sequential because a hub's
tools touch real systems (Odoo, GitHub, the filesystem) and a parallel suite
would make failures depend on ordering; one runtime because MCP init is the
expensive part and it must be opened and closed in the same asyncio task (see
`OpenAIAgentsRuntime.aopen`).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable, Iterable

from .. import _request_ctx
from . import assertions
from .cases import EvalCase
from .results import CaseResult, SuiteResult, now_iso

log = logging.getLogger("hubzoid.evals")

# Both backends swallow their own exceptions and yield this marker into the
# stream instead of raising, so a broken run arrives as text. Detect it, or a
# hub with a dead model would report every case as "failed on contains".
_AGENT_ERROR_MARKER = "[agent error:"

ProgressFn = Callable[[CaseResult], None]


def judge_tools(rt) -> list[str]:
    """This hub's tool inventory, for the judge. Never fails the run."""
    try:
        from .judge import available_tools
        return available_tools(rt)
    except Exception as exc:  # noqa: BLE001 — an unknown inventory is survivable
        log.debug("could not read the tool inventory: %s", exc)
        return []


def _chat_id(case: EvalCase) -> str:
    """Per-case chat scope, so uploads and artifacts never bleed across cases."""
    return f"eval-{case.name}"


async def _run_one(rt, case: EvalCase, *, judge_fn=None,
                   tools_available: list[str] | None = None) -> CaseResult:
    """Run a single case to a verdict. Never raises — failures become results."""
    result = CaseResult(name=case.name, tags=list(case.tags))
    started = time.monotonic()

    raw = ""
    try:
        with _request_ctx.tool_call_recorder() as calls, _request_ctx.chat_scope(_chat_id(case)):
            raw = await asyncio.wait_for(rt.run(case.prompt), timeout=case.timeout)
            result.tool_calls = [c.get("name", "?") for c in calls]
    except asyncio.TimeoutError:
        result.duration = time.monotonic() - started
        result.error = f"timed out after {case.timeout}s"
        return result
    except Exception as exc:  # noqa: BLE001 — a crashed case is a failed case
        result.duration = time.monotonic() - started
        result.error = f"{type(exc).__name__}: {exc}"
        log.exception("eval case %s crashed", case.name)
        return result

    result.duration = time.monotonic() - started
    result.response = assertions.strip_chrome(raw)

    if _AGENT_ERROR_MARKER in raw:
        # Surface the backend's own message rather than a misleading
        # assertion failure downstream.
        start = raw.index(_AGENT_ERROR_MARKER)
        result.error = raw[start:start + 200].strip()
        return result

    result.checks = assertions.run_free_checks(
        case, response=result.response, tool_calls=result.tool_calls)

    if judge_fn is not None and case.is_judged and result.free_passed:
        # Both tool lists go to the judge as observed ground truth. Criteria
        # routinely say "reports what the tool returned" or "does not invent
        # tools"; without the lists the judge guesses, and guesses wrong (see
        # judge.py for the two real misgradings this fixed).
        result.judge = await judge_fn(case, result.response, result.tool_calls,
                                      tools_available)

    return result


async def arun_suite(
    hub_dir: Path,
    cases: Iterable[EvalCase],
    *,
    judge_fn=None,
    on_case: ProgressFn | None = None,
    model: str | None = None,
) -> SuiteResult:
    """Run `cases` against `hub_dir`. Returns a SuiteResult; never raises for
    a failing case (only for a hub that cannot be built at all).

    `judge_fn(case, response) -> JudgeResult | None` is injected rather than
    imported so this module stays free of model concerns — and so the tests
    can run the whole suite path with no model at all.
    """
    from .. import runtime as runtime_lib

    cases = list(cases)
    suite = SuiteResult(hub=hub_dir.name, started_at=now_iso(),
                        judged=judge_fn is not None)

    rt = runtime_lib.build(hub_dir, model=model)
    suite.model = getattr(rt, "name", "") or ""

    # Open/use/close MCP in one task — see runtime.aopen() for why.
    await rt.aopen()
    try:
        # After aopen(), so MCP-provided tools are in the inventory too.
        tools_available = judge_tools(rt) if judge_fn is not None else None
        for case in cases:
            log.info("eval: running %s", case.name)
            result = await _run_one(rt, case, judge_fn=judge_fn,
                                    tools_available=tools_available)
            suite.cases.append(result)
            if on_case is not None:
                on_case(result)
    finally:
        await rt.aclose()

    suite.finished_at = now_iso()
    return suite


def run_suite(hub_dir: Path, cases: Iterable[EvalCase], **kwargs) -> SuiteResult:
    """Blocking wrapper for the CLI. ContextVars set by the caller are visible
    inside, because the task `asyncio.run` creates copies the current context.
    """
    return asyncio.run(arun_suite(hub_dir, cases, **kwargs))
