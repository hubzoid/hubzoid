"""The AgentCore HTTP contract inside the microVM: ``hubzoid runner serve``.

  GET  /ping         ``Healthy``, or ``HealthyBusy`` while a call runs
  POST /warmup       the platform V2 build hook: load code into memory (``memory``)
                     and read the files sessions read first (``disk``). Nothing
                     secret, no database connection, no customer tool code: the
                     snapshot taken after it is shared by every session
  POST /invocations  one call (``protocol``): claim the work order, enter the
                     turn's context, run the hub's own runtime, stream events

The runner never owns shared state (``role``): it does not migrate, schedule,
sweep or route channels. Secrets are read on the first call after the snapshot
restore, through the normal configuration layers (``HUBZOID_HUB_SECRET_NAME``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from fastapi import Request  # module level: route annotations are strings here

from . import calls, context, protocol, role

log = logging.getLogger("hubzoid.runner")
CANCEL_POLL_SECONDS = 1.0


def _version() -> str:
    from .. import __version__

    return f"{__version__} {os.environ.get('HUBZOID_IMAGE', '')}".strip()


def create_app(hub_dir: Path):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, StreamingResponse

    hub_dir = Path(hub_dir).resolve()
    app = FastAPI(title="Hubzoid AgentCore runner", docs_url=None, redoc_url=None, openapi_url=None)
    state = {"busy": 0, "served": 0, "secrets": False, "warm": {}}

    @app.get("/ping")
    async def ping():
        return {"status": "HealthyBusy" if state["busy"] else "Healthy"}

    @app.post("/warmup")
    async def warmup(request: Request):
        # Both phases on every call: AgentCore may send only one type, and the
        # file read is cheap (well under a second).
        kind = request.headers.get("x-amzn-bedrock-agentcore-runtime-warmup-type", "").lower() or "unknown"
        t0 = time.monotonic()
        cli = await asyncio.to_thread(_warm_memory, hub_dir)
        await asyncio.to_thread(_warm_disk, hub_dir)
        state["warm"][kind] = {"seconds": round(time.monotonic() - t0, 2), "claude_cli": cli}
        log.info("runner: warmup %s took %s s, Claude CLI first run: %s", kind, state["warm"][kind]["seconds"], cli)
        return JSONResponse({"ok": True})

    @app.post("/invocations")
    async def invocations(request: Request):
        body = await request.json()
        session = request.headers.get("x-amzn-bedrock-agentcore-runtime-session-id", "")
        fresh = state["served"] == 0
        state["served"] += 1
        if not state["secrets"]:
            # The first call after the snapshot restore: configuration and secrets.
            from .. import settings as settingslib

            await asyncio.to_thread(settingslib.load, hub_dir)
            state["secrets"] = True
        call_id = str(body.get("call") or "")
        mismatch = "" if body.get("kind") == "hello" else protocol.release_mismatch(body.get("release"),
                                                                                    protocol.release())
        if mismatch:
            await asyncio.to_thread(calls.refuse, hub_dir, call_id=call_id, reason=mismatch)
            _audit_refused(hub_dir, call_id, session, mismatch)
            return StreamingResponse(iter([protocol.sse({"type": "refused", "code": "version", "reason": mismatch})]),
                                     media_type="text/event-stream")
        row = await asyncio.to_thread(calls.claim, hub_dir, call_id=call_id,
                                      request_digest=protocol.digest(body), session_id=session,
                                      runner_version=_version())
        if row is None:
            reason = "no matching work order (missing, already claimed, stale, other session or changed request)"
            await asyncio.to_thread(calls.refuse, hub_dir, call_id=call_id, reason=reason)
            _audit_refused(hub_dir, call_id, session, reason)
            return StreamingResponse(iter([protocol.sse({"type": "refused", "reason": reason})]),
                                     media_type="text/event-stream")
        return StreamingResponse(_stream(hub_dir, body, row, fresh, state), media_type="text/event-stream")

    return app


def _audit_refused(hub_dir: Path, call_id: str, session: str, reason: str) -> None:
    try:
        from .. import audit_log

        audit_log.emit("runner", "runner.call_refused", hub_dir=hub_dir, hub=hub_dir.name,
                       detail={"call": call_id[:64], "session": session[:100], "reason": reason})
    except Exception:  # noqa: BLE001 - auditing never breaks the refusal
        log.warning("runner: refused call %s (%s)", call_id[:16], reason)


async def _stream(hub_dir: Path, body: dict, row: dict, fresh: bool, state: dict):
    seq = 0

    def ev(obj: dict) -> bytes:
        nonlocal seq
        seq += 1
        obj["seq"] = seq
        return protocol.sse(obj)

    state["busy"] += 1
    call_id = row["id"]
    # None until the turn's own end: a stream closed before it (the caller went
    # away, AgentCore stopped the session) was cancelled, never "ok".
    outcome, error = None, None
    try:
        yield ev({"type": "start", "v": protocol.VERSION, "fresh": fresh, "version": _version()})
        if body.get("kind") == "hello":
            yield ev({"type": "end", "outputs": {"runner_version": _version(), "protocol": protocol.VERSION,
                                                 "release": protocol.release(), "warmup": state["warm"],
                                                 "fresh": fresh}})
            outcome = "ok"
            return
        queue: asyncio.Queue = asyncio.Queue()
        worker = asyncio.create_task(_run_turn(hub_dir, body, row, queue))
        monitor = asyncio.create_task(_watch_cancel(hub_dir, call_id, worker))
        started = time.monotonic()
        try:
            while True:
                remaining = protocol.MAX_STREAM_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    worker.cancel()
                    raise TimeoutError("the turn ran past 55 minutes")
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=min(protocol.KEEPALIVE_SECONDS, remaining))
                except asyncio.TimeoutError:
                    yield protocol.KEEPALIVE
                    continue
                if item is None:
                    break
                yield ev(item)
                if item.get("type") == "end":
                    outcome = "error" if item.get("error") else ("cancelled" if item.get("cancelled") else "ok")
                    error = item.get("error")
        finally:
            monitor.cancel()
            if not worker.done():
                worker.cancel()
    except Exception as exc:  # noqa: BLE001 - the call fails, the runner keeps serving
        log.exception("runner: call %s failed", call_id)
        outcome, error = "error", f"{type(exc).__name__}: {exc}"
        yield ev({"type": "end", "outputs": {}, "error": error})
    finally:
        state["busy"] -= 1
        if outcome is None:
            outcome, error = "cancelled", error or "the stream closed before the turn ended"
        with contextlib.suppress(Exception):
            await asyncio.to_thread(calls.end, hub_dir, call_id, outcome=outcome, error=error)


async def _watch_cancel(hub_dir: Path, call_id: str, worker: asyncio.Task) -> None:
    """Stop the turn within a second or two of the hub service asking, or once
    the hub service stopped renewing the call (it went away)."""
    while not worker.done():
        await asyncio.sleep(CANCEL_POLL_SECONDS)
        try:
            go_on = await asyncio.to_thread(calls.running, hub_dir, call_id)
        except Exception:  # noqa: BLE001 - unknown: keep going, the tool guard still checks
            continue
        if not go_on:
            worker.cancel()
            return


async def _run_turn(hub_dir: Path, body: dict, row: dict, queue: asyncio.Queue) -> None:
    """Run one turn with the hub's own runtime and put its events on ``queue``.
    Whatever happens, it ends with exactly one ``end`` event and then None."""
    from .. import run_events
    from .. import runtime as runtime_lib

    call_id = row["id"]
    opts = body.get("options") or {}
    runlog: list[dict] = []
    outputs: dict = {}
    cancelled = False
    failure: str | None = None

    def guard() -> bool:
        return calls.running(hub_dir, call_id)

    def emit(**fields) -> None:
        runlog.append(fields)

    try:
        extra = None
        bundle = opts.get("bundle")
        if bundle and bundle.get("kind") == "schedule":
            extra = await asyncio.to_thread(_schedule_tools, hub_dir, bundle, emit)
        rt = await asyncio.to_thread(runtime_lib._build, hub_dir, extra_tools=extra,
                                     max_turns=opts.get("max_turns"), model=opts.get("model"))
        with context.restore(body.get("context") or {}, row.get("identity"), guard) as slot:
            seen_waiting = 0
            stream = None
            try:
                await rt.aopen()
                stream = run_events.stream_items(rt, str(body.get("prompt") or ""))
                async for item in stream:
                    while runlog:
                        await queue.put({"type": "runlog", "fields": runlog.pop(0)})
                    waiting = list(getattr(slot, "hitl_waiting", None) or []) if slot is not None else []
                    if len(waiting) > seen_waiting and type(item).__name__ == "ToolResult":
                        await queue.put({"type": "hitl_waiting", "notes": waiting[seen_waiting:]})
                        seen_waiting = len(waiting)
                    enc = protocol.encode_item(item)
                    if enc is not None:
                        await queue.put(enc)
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:  # noqa: BLE001 - reported in the end event
                log.exception("runner: call %s failed in its turn", call_id)
                failure = f"{type(exc).__name__}: {exc}"
            finally:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        await run_events.aclose(stream)
                with contextlib.suppress(Exception):
                    await rt.aclose()
                err = getattr(rt, "last_error", None)
                if err is not None and failure is None:
                    failure = f"{type(err).__name__}: {err}"
                outputs = context.outputs(slot)
    except asyncio.CancelledError:
        cancelled = True
    except Exception as exc:  # noqa: BLE001 - building the turn failed
        log.exception("runner: call %s failed before its turn", call_id)
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if cancelled:
            # The hub service no longer reads this call: its budget hold would
            # stay open. Settling is idempotent (only an open hold changes).
            _settle_hold(hub_dir, outputs)
        while runlog:
            queue.put_nowait({"type": "runlog", "fields": runlog.pop(0)})
        queue.put_nowait({"type": "end", "outputs": outputs, "cancelled": cancelled, "error": failure})
        queue.put_nowait(None)


def _settle_hold(hub_dir: Path, outputs: dict) -> None:
    reservation = ((outputs or {}).get("usage") or {}).get("budget_reservation")
    if not reservation:
        return
    try:
        from .. import budgets, db

        with db.operational_engine(hub_dir).begin() as conn:
            budgets.settle_in(conn, reservation)
    except Exception:  # noqa: BLE001 - the budget page shows an open hold; never fail the stop
        log.warning("runner: could not settle budget hold %s", reservation, exc_info=True)


def _schedule_tools(hub_dir: Path, bundle: dict, emit) -> dict:
    """A scheduled task's own tools, rebuilt here from its name."""
    from .. import scheduling
    from ..tools import schedule_tools

    tasks, _ = scheduling.load_tasks(hub_dir)
    task = next((t for t in tasks if t.name == bundle.get("task")), None)
    if task is None:
        raise LookupError(f"schedule/{bundle.get('task')}.md is not in this runner's hub")
    for key in ("timeout", "max_rounds", "model", "max_turns"):
        value = (bundle.get("overrides") or {}).get(key)
        if value:
            setattr(task, key, value)
    run = bundle.get("run") or {}
    task.state_rel = run.get("state_rel") or None
    task.run_identity = run.get("run_identity") or None
    task.run_id = run.get("run_id") or None
    return {t.name: t for t in schedule_tools.make(hub_dir, task, emit)}


def _warm_memory(hub_dir: Path) -> dict:
    """Imports every session needs. Never secrets, AWS clients, database
    connections or the hub's own tool code (customer code) before the snapshot."""
    import boto3  # noqa: F401
    import botocore  # noqa: F401

    from .. import access, factory, run_events, runtime  # noqa: F401
    from ..chat import stream  # noqa: F401
    try:
        from .. import factory_claude  # noqa: F401
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        pass
    return _cli_first_run()


def _cli_first_run() -> dict:
    """The Claude CLI's own first start (version check), so its files are
    warm. Returns what happened, for the log and ``hello``."""
    import subprocess

    t0 = time.monotonic()
    try:
        import claude_agent_sdk

        base = Path(claude_agent_sdk.__file__).parent
        cli = next((p for p in base.rglob("claude") if p.is_file() and os.access(p, os.X_OK)), None)
        if cli is None:
            return {"ran": False, "why": "no Claude CLI in claude_agent_sdk"}
        out = subprocess.run([str(cli), "--version"], capture_output=True, text=True, timeout=90, check=False)
        return {"ran": True, "code": out.returncode, "seconds": round(time.monotonic() - t0, 2),
                "out": (out.stdout or out.stderr or "").strip()[-200:]}
    except Exception as exc:  # noqa: BLE001 - warming is best effort
        return {"ran": False, "why": f"{type(exc).__name__}: {exc}"[:200], "seconds": round(time.monotonic() - t0, 2)}


#: What the disk warmup reads of the hub: its instructions, knowledge, skills
#: and code. Never configuration (.env), restricted/, state or data folders.
_WARM_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".json", ".txt"}
_WARM_SKIP = {"restricted", "output", "logs", "raw_data", "node_modules", "e2e", "deploy"}


def _warm_files(hub_dir: Path):
    for p in hub_dir.rglob("*"):
        rel = p.relative_to(hub_dir).parts
        if any(part.startswith(".") or part in _WARM_SKIP for part in rel):
            continue
        if p.suffix.lower() in _WARM_SUFFIXES and p.is_file() and not p.is_symlink():
            yield p


def _warm_disk(hub_dir: Path) -> None:
    """Read the files each session reads when it starts: the hub's own (see
    ``_WARM_SUFFIXES``) and the Claude SDK's. Nothing secret."""
    files = list(_warm_files(hub_dir))
    try:
        import claude_agent_sdk

        files += [p for p in Path(claude_agent_sdk.__file__).parent.rglob("*") if p.is_file() and not p.is_symlink()]
    except ImportError:
        pass
    for p in files:
        with contextlib.suppress(OSError), p.open("rb") as fh:
            while fh.read(1 << 20):
                pass


def serve(hub_dir: Path, *, port: int = 8080) -> None:
    """``hubzoid runner serve``: run the contract on 0.0.0.0 (AgentCore's network)."""
    import uvicorn

    os.environ.setdefault(role.ENV, role.RUNNER)
    if role.current() != role.RUNNER:
        raise SystemExit(f"hubzoid runner serve needs {role.ENV}=runner (it is {role.current()!r}).")
    uvicorn.run(create_app(hub_dir), host="0.0.0.0", port=port, log_level="info", proxy_headers=False)
