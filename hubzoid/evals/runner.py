"""Execute eval cases against the hub's own runtime.

The whole point of this module is that it does **not** have a special
execution path. A case runs through `runtime.build(hub)` — the same call the
FastAPI bridge and `hubzoid test` make — so it sees the same model, the same
tools, the same MCP servers, the same skills, the same access guard. Anything
else would be testing a simulation of the hub instead of the hub.

Each turn runs on the typed event stream the web app consumes
(`run_events.stream_items`), so every runtime reports its tool calls the same
way: name, arguments, outcome, and, where the runtime records it, the start
of the result (`_request_ctx.tool_result_recorder`). The reply text is the
same 1.0.x text `rt.run` returns (`run_events.text_of` of every item), so
`strip_chrome` and the agent-error detection see exactly what they did
before. A runtime with neither `stream_events` nor `stream` (a plug-in or a
test double) is run with `rt.run`, and its calls come from
`_request_ctx.tool_call_recorder` without outcomes.

A multi-turn case (`## Turn 1`, `## Turn 2`, ...) runs every turn in one chat
scope. Each turn's prompt is built by the web app's own history builder
(`chat.history.build_prompt`) from the earlier user turns and the agent's real
earlier replies (the text the web app would store), so an eval sees what a web
conversation sees. A case with `run_as` (or a suite default) runs as that
account, resolved with the workflow identity rules and bound the way a
workflow run binds its account (surface `workflow`); it grants nothing.

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
import contextlib
import inspect
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .. import _request_ctx, run_events
from . import assertions
from . import calls as calls_lib
from .cases import EvalCase
from .results import CaseResult, SuiteResult, ToolCallRecord, TurnRecord, now_iso

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


# --------------------------------------------------------------------------
# One turn
# --------------------------------------------------------------------------
@dataclass
class _Turn:
    raw: str = ""        # the 1.0.x text: exactly what `rt.run` returns
    answer: str = ""     # what the web app stores as the reply's text
    calls: list[ToolCallRecord] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)   # runtime id per call
    typed: bool = False  # the stream reported tool calls itself


def _streams(rt) -> bool:
    return (getattr(rt, "stream_events", None) is not None
            or getattr(rt, "stream", None) is not None)


async def _consume(rt, prompt: str, turn_no: int, out: _Turn) -> None:
    """Run one turn, filling `out` as items arrive, so a timeout or a crash
    keeps what was seen."""
    if not _streams(rt):
        out.raw = await rt.run(prompt)
        out.answer = out.raw
        return
    raw: list[str] = []
    answer: list[str] = []
    pending: list[tuple[str, ToolCallRecord, float]] = []   # calls awaiting a result
    stream = run_events.stream_items(rt, prompt)
    try:
        async for item in stream:
            raw.append(run_events.text_of(item))
            if isinstance(item, str):
                answer.append(item)
            elif isinstance(item, run_events.Notice):
                # The download footer is answer text in the web app; an error
                # never is.
                if item.kind != "error":
                    answer.append(item.legacy or item.text)
            elif isinstance(item, run_events.ToolCall):
                out.typed = True
                rec = ToolCallRecord(name=item.name, args=calls_lib.safe_args(item.args),
                                     turn=turn_no, raw_args=calls_lib.normalize_args(item.args))
                call_id = str(item.id or "")
                out.calls.append(rec)
                out.call_ids.append(call_id)
                pending.append((call_id, rec, time.monotonic()))
            elif isinstance(item, run_events.ToolResult):
                _close_call(pending, item)
    finally:
        await run_events.aclose(stream)
        out.raw = "".join(raw)
        out.answer = "".join(answer)


def _close_call(pending: list, item: run_events.ToolResult) -> None:
    """Match a result to its call: by id, else the latest open call of that
    tool, else the latest open call (as the web app's MessageBuilder does)."""
    rid = str(item.id or "")
    index = next((i for i, (cid, _, _) in enumerate(pending) if rid and cid == rid), None)
    if index is None:
        named = [i for i, (_, rec, _) in enumerate(pending) if rec.name == item.name]
        index = (named or list(range(len(pending))) or [None])[-1]
    if index is None:
        return
    _, rec, started = pending.pop(index)
    rec.ok = bool(item.ok)
    rec.duration_ms = int((time.monotonic() - started) * 1000)
    if not item.ok:
        rec.error = calls_lib.error_text(item.message or run_events.TOOL_FAILED)


def _finish_turn(turn: _Turn, turn_no: int, recorded: list, results: dict) -> None:
    """Attach result previews (and real error text) recorded by the runtime;
    for a runtime that reported no typed calls, take the recorder's calls."""
    if not turn.typed:
        turn.calls = [ToolCallRecord(name=str(c.get("name") or "?"),
                                     args=calls_lib.safe_args(c.get("args")), turn=turn_no,
                                     raw_args=calls_lib.normalize_args(c.get("args")))
                      for c in recorded]
        turn.call_ids = [""] * len(turn.calls)
    for rec, call_id in zip(turn.calls, turn.call_ids):
        text = results.get(call_id) if call_id else None
        if text is None:
            continue
        if rec.ok is False:
            rec.error = calls_lib.error_text(text) or rec.error
        else:
            rec.preview = calls_lib.preview(text)


def _turn_prompt(hub_dir: Path, case: EvalCase, done: list[_Turn], text: str) -> str:
    """A turn's prompt as the web app builds it: the branch of earlier user
    turns and the agent's real replies, then this turn, flattened by
    `chat.history.build_prompt`."""
    from ..chat import history

    branch: list[dict] = []
    for said, turn in zip(case.turns, done):
        branch.append({"role": "user", "content": [{"type": "text", "text": said}]})
        branch.append({"role": "assistant", "content": [{"type": "text", "text": turn.answer}]})
    branch.append({"role": "user", "content": [{"type": "text", "text": text}]})
    return history.build_prompt(hub_dir, _chat_id(case), branch)


# --------------------------------------------------------------------------
# Identity (run_as)
# --------------------------------------------------------------------------
def _what(case: EvalCase) -> str:
    return f"Eval case {case.name!r}"


def _resolve_identity(hub_dir: Path, email: str, case: EvalCase):
    """The account `email` as a RunIdentity, by the workflow identity rules: a
    usable account (bound, not pending, not blocked, holding use_hub on a
    managed hub). Raises IdentityError naming the fix. Grants nothing."""
    from ..workflows import identity as idlib

    return idlib.resolve(Path(hub_dir), run_as=email, what=_what(case))


def _recheck_identity(hub_dir: Path, ident, case: EvalCase) -> None:
    from ..workflows import identity as idlib

    idlib.recheck(Path(hub_dir), Path(hub_dir).name.lower(), ident, what=_what(case))


def _identity_scope(ident):
    """Bind the account the way a workflow run does (`workflows.context.run_scope`,
    `schedule_runner.run_task`): the person, no groups, surface `workflow`."""
    if ident is None:
        return contextlib.nullcontext()
    from ..access import Identity, identity_scope

    return identity_scope(Identity.make(ident.subject, surface="workflow"))


def _judge_extras(judge_fn) -> set[str]:
    """Which of the newer keyword arguments (`tools`, `turns`) an injected
    judge accepts. An older judge_fn(case, response, tool_calls,
    tools_available) keeps working with names only."""
    try:
        params = inspect.signature(judge_fn).parameters
    except (TypeError, ValueError):
        return set()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return {"tools", "turns"}
    return {n for n in ("tools", "turns") if n in params}


# --------------------------------------------------------------------------
# One case
# --------------------------------------------------------------------------
async def _run_one(rt, case: EvalCase, *, judge_fn=None,
                   tools_available: list[str] | None = None,
                   hub_dir: Path | None = None,
                   run_as: str | None = None) -> CaseResult:
    """Run a single case to a verdict. Never raises — failures become results."""
    result = CaseResult(name=case.name, tags=list(case.tags))
    started = time.monotonic()
    hub_dir = Path(hub_dir) if hub_dir is not None else Path(".")

    ident = None
    who = case.run_as or run_as
    if who:
        result.run_as = who
        try:
            ident = await asyncio.to_thread(_resolve_identity, hub_dir, who, case)
        except Exception as exc:  # noqa: BLE001 — no usable account: the case fails
            result.duration = time.monotonic() - started
            result.error = str(exc) or type(exc).__name__
            return result
        result.run_as = ident.subject

    done: list[_Turn] = []
    names: list[str] = []
    turn_no = 0
    try:
        with _identity_scope(ident), _request_ctx.chat_scope(_chat_id(case)):
            for turn_no, text in enumerate(case.prompts, start=1):
                if turn_no > 1 and ident is not None:
                    # The account may have been blocked since the last turn.
                    await asyncio.to_thread(_recheck_identity, hub_dir, ident, case)
                prompt = _turn_prompt(hub_dir, case, done, text) if case.is_multi_turn else text
                turn = _Turn()
                done.append(turn)
                with (_request_ctx.tool_call_recorder() as recorded,
                      _request_ctx.tool_result_recorder() as results):
                    try:
                        await asyncio.wait_for(_consume(rt, prompt, turn_no, turn),
                                               timeout=case.timeout)
                    finally:
                        names += [c.get("name", "?") for c in recorded]
                        _finish_turn(turn, turn_no, recorded, results)
                if _AGENT_ERROR_MARKER in turn.raw:
                    break
    except asyncio.TimeoutError:
        _record(result, case, done, names, started)
        result.error = f"timed out after {case.timeout}s" + _on_turn(case, turn_no)
        return result
    except Exception as exc:  # noqa: BLE001 — a crashed case is a failed case
        _record(result, case, done, names, started)
        result.error = f"{type(exc).__name__}: {exc}" + _on_turn(case, turn_no)
        log.exception("eval case %s crashed", case.name)
        return result

    _record(result, case, done, names, started)
    final = done[-1].raw if done else ""
    result.response = assertions.strip_chrome(final)

    if _AGENT_ERROR_MARKER in final:
        # Surface the backend's own message rather than a misleading
        # assertion failure downstream.
        start = final.index(_AGENT_ERROR_MARKER)
        result.error = final[start:start + 200].strip()
        if case.is_multi_turn:
            result.error = f"turn {len(done)}: {result.error}"
        return result

    result.checks = assertions.run_free_checks(
        case, response=result.response, tool_calls=result.tool_calls, tools=result.tools)

    if judge_fn is not None and case.is_judged and result.free_passed:
        # Both tool lists go to the judge as observed ground truth. Criteria
        # routinely say "reports what the tool returned" or "does not invent
        # tools"; without the lists the judge guesses, and guesses wrong (see
        # judge.py for the two real misgradings this fixed). The calls go
        # with their arguments and outcomes when the judge takes them.
        extras = _judge_extras(judge_fn)
        kwargs = {}
        if "tools" in extras:
            kwargs["tools"] = result.tools
        if "turns" in extras:
            kwargs["turns"] = result.turns
        result.judge = await judge_fn(case, result.response, result.tool_calls,
                                      tools_available, **kwargs)

    return result


def _on_turn(case: EvalCase, turn_no: int) -> str:
    return f" on turn {turn_no}" if case.is_multi_turn and turn_no else ""


def _record(result: CaseResult, case: EvalCase, done: list[_Turn], names: list[str],
            started: float) -> None:
    """Copy what the turns produced onto the result (also for a partial run)."""
    result.duration = time.monotonic() - started
    result.tool_calls = list(names)
    result.tools = [call for turn in done for call in turn.calls]
    if case.is_multi_turn:
        result.turns = [TurnRecord(prompt=text, response=assertions.strip_chrome(turn.raw))
                        for text, turn in zip(case.turns, done)]


# --------------------------------------------------------------------------
# A suite
# --------------------------------------------------------------------------
async def arun_suite(
    hub_dir: Path,
    cases: Iterable[EvalCase],
    *,
    judge_fn=None,
    on_case: ProgressFn | None = None,
    model: str | None = None,
    run_as: str | None = None,
    trigger: str = "cli",
) -> SuiteResult:
    """Run `cases` against `hub_dir`. Returns a SuiteResult; never raises for
    a failing case (only for a hub that cannot be built at all).

    `judge_fn(case, response, tool_calls, tools_available, *, tools, turns)
    -> JudgeResult | None` is injected rather than imported so this module
    stays free of model concerns — and so the tests can run the whole suite
    path with no model at all. `run_as` is the account a case without its own
    `run_as` runs as (None: as today, no account bound). `trigger` is recorded
    in the results file.
    """
    from .. import runtime as runtime_lib

    cases = list(cases)
    suite = SuiteResult(hub=hub_dir.name, started_at=now_iso(),
                        judged=judge_fn is not None, trigger=trigger, run_as=run_as)

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
                                    tools_available=tools_available,
                                    hub_dir=hub_dir, run_as=run_as)
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
