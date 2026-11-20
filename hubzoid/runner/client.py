"""``AgentCoreRuntime``: the hub service's side of the AgentCore runner.

``runtime.build()`` returns this when a hub sets ``HUBZOID_RUNNER=agentcore``.
It is an ordinary ``Runtime``: chat, the OpenAI-compatible API (Slack and the
other channels), scheduled tasks and workflow ``hub.call_agent`` use it
unchanged. For each turn it:

  1. captures the turn's context (``runner.context``) and the caller,
  2. writes a work order (``runner.calls``) bound to the exact request,
  3. invokes the runtime (``InvokeAgentRuntime``, or a plain HTTP runner in tests)
     with the AgentCore session id of the chat or run,
  4. turns the runner's events back into ``run_events`` items, and puts the
     runner's outputs (usage with its budget hold, session, artifacts, run
     failure, approval notes) into this process's context as if the turn had
     run here.

Stopping: when the turn is cancelled here, or the chat run's own guard says
stop, the work order is set to ``cancel_requested``; the runner stops at its
next tool step and within a second or two of its model call. If it does not
end in 15 seconds, ``StopRuntimeSession`` ends the microVM (when no other call
of the session is open). A dropped connection alone never stops a turn on
AgentCore, which is why this exists. While it waits, this process renews the
call; if it goes away, the runner stops the call by itself.

There is no fallback: a runner failure ends the turn with a clear error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from . import calls, context, protocol

log = logging.getLogger("hubzoid.runner")

#: Streams block a thread each; keep them off the default executor that the
#: database calls use.
_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="agentcore")
IDLE_SECONDS = 60.0          # no event or keep-alive for this long: the runner is gone
STOP_GRACE_SECONDS = 15.0    # after cancel_requested, before StopRuntimeSession
GUARD_POLL_SECONDS = 2.0      # the chat run's guard; the call is renewed every calls.HEARTBEAT_SECONDS

ERRORS = {
    "runner_unavailable": "This agent runs on AWS AgentCore, which did not answer. Nothing ran.",
    "runner_quota": "AWS refused a new session for this agent (quota). Nothing ran.",
    "runner_throttled": "AWS is limiting requests to this agent right now. Try again in a moment.",
    "runner_failed": "This agent's runner on AWS AgentCore stopped with an error. Check its CloudWatch log.",
    "runner_timeout": "This agent's runner on AWS AgentCore stopped answering.",
    "runner_version": "This agent's runner on AWS AgentCore runs a different Hubzoid version.",
    "call_refused": "The AgentCore runner refused this call.",
    "runner_config": "This agent is set to run on AWS AgentCore but is not configured.",
}


class RunnerError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(ERRORS.get(code, code) + (f" ({detail})" if detail else ""))


# -- transport --------------------------------------------------------------------
class _Transport:
    """Invoke a runner and read its event stream (blocking: runs on ``_POOL``)."""

    def __init__(self, settings):
        self.settings = settings
        self.arn = settings.agentcore_runtime_arn
        self.qualifier = settings.agentcore_qualifier or "DEFAULT"
        self.endpoint = (settings.agentcore_endpoint or "").rstrip("/") or None
        self._local = threading.local()

    def _region(self) -> str | None:
        if self.settings.agentcore_region:
            return self.settings.agentcore_region
        parts = (self.arn or "").split(":")
        return parts[3] if len(parts) > 3 and parts[3] else None

    def _client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            import boto3
            from botocore.config import Config

            # No automatic retries: a work order is claimed once, so only errors
            # that prove the request never reached the runner are retried (below).
            client = boto3.Session().client(
                "bedrock-agentcore", region_name=self._region(),
                config=Config(read_timeout=protocol.MAX_STREAM_SECONDS + 300, connect_timeout=10,
                              retries={"max_attempts": 1, "mode": "standard"}))
            self._local.client = client
        return client

    def invoke(self, session_id: str, request: dict, stop: threading.Event):
        """Yield the runner's events. Raises RunnerError."""
        body = json.dumps(request, default=str).encode()
        if self.endpoint:
            yield from self._invoke_http(session_id, body, stop)
            return
        if not self.arn:
            raise RunnerError("runner_config", "HUBZOID_AGENTCORE_RUNTIME_ARN is not set")
        from botocore.exceptions import BotoCoreError, ClientError

        delay = 0.25
        deadline = time.monotonic() + 20.0
        while True:
            try:
                resp = self._client().invoke_agent_runtime(
                    agentRuntimeArn=self.arn, qualifier=self.qualifier, runtimeSessionId=session_id,
                    payload=body, contentType="application/json", accept="text/event-stream")
                break
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                # Never reached the runner: safe to send the same work order again.
                if code in ("RetryableConflictException", "ThrottlingException") and time.monotonic() < deadline:
                    time.sleep(delay)
                    delay = min(delay * 2, 4.0)
                    continue
                raise RunnerError(_code_for(code), code) from None
            except BotoCoreError as exc:
                raise RunnerError("runner_unavailable", type(exc).__name__) from None
        raw = resp["response"]._raw_stream
        yield from protocol.iter_sse(_read_available(raw, stop))

    def _invoke_http(self, session_id: str, body: bytes, stop: threading.Event):
        import httpx

        try:
            with httpx.Client(timeout=httpx.Timeout(10.0, read=IDLE_SECONDS + 5)) as http:
                with http.stream("POST", self.endpoint + "/invocations", content=body, headers={
                        "Content-Type": "application/json", "Accept": "text/event-stream",
                        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id}) as resp:
                    if resp.status_code >= 400:
                        raise RunnerError("runner_failed", f"HTTP {resp.status_code}")
                    yield from protocol.iter_sse(_until_stopped(resp.iter_raw(), stop))
        except httpx.HTTPError as exc:
            raise RunnerError("runner_unavailable", type(exc).__name__) from None

    def stop_session(self, session_id: str) -> None:
        if self.endpoint or not self.arn:
            return
        try:
            self._client().stop_runtime_session(agentRuntimeArn=self.arn, qualifier=self.qualifier,
                                                runtimeSessionId=session_id)
        except Exception:  # noqa: BLE001 - best effort; the idle timeout ends it anyway
            log.warning("agentcore: could not stop session %s", session_id[:24], exc_info=True)


def _code_for(aws_code: str) -> str:
    return {"ServiceQuotaExceededException": "runner_quota", "ThrottlingException": "runner_throttled",
            "RuntimeClientError": "runner_failed", "AccessDeniedException": "runner_unavailable",
            "ResourceNotFoundException": "runner_config", "ValidationException": "runner_config",
            }.get(aws_code, "runner_unavailable")


def _read_available(raw, stop: threading.Event):
    """Bytes as they arrive. botocore's iter_chunks waits for 1 KB, which would
    hold events back."""
    while not stop.is_set():
        chunk = raw.read1(65536) if hasattr(raw, "read1") else raw.read(1024)
        if not chunk:
            return
        yield chunk


def _until_stopped(chunks, stop: threading.Event):
    for chunk in chunks:
        if stop.is_set():
            return
        yield chunk


_TRANSPORTS: dict[tuple, _Transport] = {}


def transport(settings) -> _Transport:
    key = (settings.agentcore_runtime_arn, settings.agentcore_qualifier, settings.agentcore_endpoint,
           settings.agentcore_region)
    t = _TRANSPORTS.get(key)
    if t is None:
        t = _TRANSPORTS[key] = _Transport(settings)
    return t


# -- the runtime --------------------------------------------------------------------
class AgentCoreRuntime:
    """A ``Runtime`` whose turns run on an AgentCore runner."""

    def __init__(self, hub_dir: Path, settings, *, model_id: str, max_turns: int | None = None,
                 model: str | None = None, bundle: dict | None = None, kind: str = "turn"):
        self.hub_dir = Path(hub_dir).resolve()
        self.settings = settings
        self.name = model_id
        self.max_turns = max_turns
        self.model = model
        self.bundle = bundle
        self.kind = kind
        self.last_error: Exception | None = None

    async def aopen(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def run(self, prompt: str) -> str:
        from .. import run_events

        return await run_events.answer_only(self, prompt)

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        from .. import run_events

        async for text in run_events.as_text(self.stream_events(prompt)):
            yield text

    def _request(self, prompt: str) -> tuple[dict, dict, str]:
        from ..access import current_identity

        ctx = context.capture()
        ident = context.identity_dict(current_identity())
        bundle = None
        if self.bundle:
            bundle = {k: v for k, v in self.bundle.items() if k != "emit"}
        request = {"v": protocol.VERSION, "kind": self.kind, "prompt": prompt, "context": ctx,
                   "release": protocol.release(),
                   "options": {"max_turns": self.max_turns, "model": self.model, "bundle": bundle}}
        key = ctx.get("chat") or ctx.get("run_tag") or f"call:{uuid.uuid4().hex}"
        return request, ident, protocol.session_id(self.hub_dir.name, key)

    async def stream_events(self, prompt: str) -> AsyncIterator[Any]:
        from .. import _request_ctx

        self.last_error = None
        request, ident, session = self._request(prompt)
        call_id = calls.new_id()
        request["call"] = call_id
        await asyncio.to_thread(calls.create, self.hub_dir, call_id=call_id, hub=self.hub_dir.name,
                                kind=self.kind, session_id=session,
                                request_digest=protocol.digest(request), identity=ident)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()
        tp = transport(self.settings)

        def pump():
            try:
                for event in tp.invoke(session, request, stop):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except RunnerError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, {"type": "_error", "code": exc.code, "detail": exc.detail})
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, {"type": "_error", "code": "runner_unavailable",
                                                             "detail": type(exc).__name__})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(_POOL, pump)
        slot = _request_ctx.native_session()
        watcher = asyncio.create_task(self._watch(slot, call_id))
        finished = False
        saw_end = False
        saw_error_notice = False
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=IDLE_SECONDS)
                except asyncio.TimeoutError:
                    raise RunnerError("runner_timeout") from None
                if event is None:
                    if not saw_end:
                        raise RunnerError("runner_failed", "the stream ended before the turn did")
                    finished = True
                    break
                kind = event.get("type")
                if kind == "_error":
                    raise RunnerError(event.get("code") or "runner_unavailable", event.get("detail") or "")
                if kind == "refused":
                    code = "runner_version" if event.get("code") == "version" else "call_refused"
                    raise RunnerError(code, str(event.get("reason") or "")[:200])
                if kind in ("keepalive", "start"):
                    if kind == "start" and event.get("v") not in (None, protocol.VERSION):
                        raise RunnerError("runner_version", f"protocol {event.get('v')}")
                    continue
                if kind == "hitl_waiting":
                    if slot is not None:
                        pending = list(getattr(slot, "hitl_waiting", None) or [])
                        pending.extend(event.get("notes") or [])
                        slot.hitl_waiting = pending
                    continue
                if kind == "runlog":
                    emit = (self.bundle or {}).get("emit")
                    if callable(emit):
                        fields = event.get("fields") or {}
                        try:
                            emit(**fields)
                        except Exception:  # noqa: BLE001 - the run log never stops a turn
                            log.warning("agentcore: run log event dropped", exc_info=True)
                    continue
                if kind == "end":
                    saw_end = True
                    context.apply(event.get("outputs") or {})
                    if event.get("error") or event.get("cancelled"):
                        # The turn did not finish there. Say so here, as a local
                        # runtime's failed turn does (a stop asked here never
                        # gets this far: the stream is no longer read).
                        detail = str(event.get("error") or "stopped on the runner")[:300]
                        self.last_error = RunnerError("runner_failed", detail)
                        _request_ctx.note_usage(status="error")
                        if not saw_error_notice:
                            saw_error_notice = True
                            yield _notice(str(self.last_error))
                    continue
                item = protocol.decode_item(event)
                if item is None or item == "":
                    continue
                if getattr(item, "kind", None) == "error":
                    saw_error_notice = True
                yield item
        except RunnerError as exc:
            self.last_error = exc
            _request_ctx.note_usage(status="error")
            # Whatever the runner still does for this call stops too.
            stop.set()
            asyncio.get_running_loop().run_in_executor(_POOL, self._stop_call, call_id, session)
            yield _notice(str(exc))
            finished = True
        finally:
            watcher.cancel()
            if not finished:
                # Cancelled here (Stop, a deadline, a lost lease): stop it there too.
                stop.set()
                asyncio.get_running_loop().run_in_executor(_POOL, self._stop_call, call_id, session)

    async def _watch(self, slot, call_id: str) -> None:
        """While this process waits: renew the call (``calls.beat``), and when
        the chat run's own guard says stop (stopped, lost its branch lease),
        cancel it there too. A runner whose call is no longer renewed stops it
        by itself (``calls.LIVE_SECONDS``)."""
        guard = getattr(slot, "guard", None)
        last_beat = time.monotonic()
        while True:
            await asyncio.sleep(GUARD_POLL_SECONDS)
            if guard is not None:
                try:
                    ok = await asyncio.to_thread(guard)
                except Exception:  # noqa: BLE001
                    ok = False
                if not ok:
                    await asyncio.to_thread(calls.request_cancel, self.hub_dir, call_id)
                    return
            if time.monotonic() - last_beat >= calls.HEARTBEAT_SECONDS:
                last_beat = time.monotonic()
                try:
                    if not await asyncio.to_thread(calls.beat, self.hub_dir, call_id):
                        return
                except Exception:  # noqa: BLE001 - the next renewal tries again
                    log.warning("agentcore: renewing call %s failed", call_id, exc_info=True)

    def _stop_call(self, call_id: str, session: str) -> None:
        """Stop a call this process gave up on. The runner stops its model and
        refuses further tool steps once the work order says so. Only a call
        that has not ended after ``STOP_GRACE_SECONDS`` (a tool still running in
        a thread) has its microVM ended (``StopRuntimeSession``), and only when
        no other call of the same session is open."""
        try:
            calls.request_cancel(self.hub_dir, call_id)
            if calls.state(self.hub_dir, call_id) in (None, calls.TERMINAL):
                return
            end = time.monotonic() + STOP_GRACE_SECONDS
            while time.monotonic() < end:
                if calls.state(self.hub_dir, call_id) == calls.TERMINAL:
                    return
                time.sleep(0.5)
            if calls.active_in_session(self.hub_dir, session, besides=call_id) == 0:
                transport(self.settings).stop_session(session)
            calls.end(self.hub_dir, call_id, outcome="cancelled", error="stopped: the runner did not end it in time")
        except Exception:  # noqa: BLE001
            log.warning("agentcore: stopping call %s failed", call_id, exc_info=True)


def _notice(text: str):
    from ..run_events import Notice

    # ``legacy`` is what text-only callers (the /v1 API, channels) show.
    return Notice(kind="error", text=text, legacy=f"\n\n{text}")


def hello(hub_dir: Path, settings) -> dict:
    """Ask the runner for its version and image, without a model call."""
    from .. import __version__

    request = {"v": protocol.VERSION, "kind": "hello", "prompt": "", "context": {}, "options": {},
               "release": protocol.release()}
    call_id = calls.new_id()
    request["call"] = call_id
    session = protocol.session_id(Path(hub_dir).name, f"hello:{uuid.uuid4().hex}")
    calls.create(hub_dir, call_id=call_id, hub=Path(hub_dir).name, kind="hello", session_id=session,
                 request_digest=protocol.digest(request), identity=None)
    t0 = time.monotonic()
    out: dict = {"hub_version": __version__}
    for event in transport(settings).invoke(session, request, threading.Event()):
        if event.get("type") == "refused":
            raise RunnerError("call_refused", str(event.get("reason") or "")[:200])
        if event.get("type") == "end":
            out.update(event.get("outputs") or {})
    out["seconds"] = round(time.monotonic() - t0, 2)
    out["hub_release"] = request["release"]
    out["same_release"] = out.get("release") == request["release"]
    transport(settings).stop_session(session)
    return out
