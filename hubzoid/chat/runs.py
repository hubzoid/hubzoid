"""Runs: each assistant reply is a server task that outlives the HTTP request.

``RunManager.start`` begins a reply as an asyncio task on the bridge's event
loop. The task:

  * counts in the bridge's in-flight gauge (the scheduler's idle gate),
  * binds the conversation as the chat scope (``_request_ctx.chat_scope``; the
    per-chat files folder is keyed by the conversation id) and the signed-in
    person as the caller (``access.identity_scope``, surface ``web``),
  * iterates ``runtime.stream_events(prompt)`` through a ``MessageBuilder``,
    handing every UI stream chunk to the subscribers (the SSE response that
    started it; a subscriber that goes away never stops the run),
  * keeps the assistant message current in the store: 'running' from the
    start, content written at most about once a second and at every tool
    event, then 'complete', 'cancelled' or 'error',
  * records one usage row like the bridge's own chat turns (surface ``web``).

``cancel(message_id)`` stops a reply; the partial content is kept. When the
bridge starts, replies of its hub still marked 'running' belong to a process
that is gone: ``sweep`` marks them failed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

from .. import _request_ctx, access, run_events
from ..run_events import Notice, ToolCall, ToolResult
from .stream import DONE, KEEPALIVE, MessageBuilder, encode

log = logging.getLogger("hubzoid.chat.runs")

INTERRUPTED = "Interrupted when the server restarted."
PERSIST_INTERVAL = 1.0
KEEPALIVE_SECONDS = 15.0
_END = object()


@dataclass
class Run:
    message_id: str
    conversation_id: str
    owner_id: str
    email: str
    builder: MessageBuilder
    loop: asyncio.AbstractEventLoop
    started: float
    task: asyncio.Task | None = None
    status: str = "running"
    history: list = field(default_factory=list)
    subscribers: set = field(default_factory=set)
    last_persist: float = 0.0
    # True once the stream has ended for subscribers ('finish' sent).
    closed: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def finished(self) -> bool:
        return self.status != "running"


def web_identity(hub_dir, email: str):
    """The caller of a web app turn: the signed-in person, their groups."""
    try:
        groups = access.effective_groups(hub_dir, email=email, surface="web", header_groups=None)
    except Exception:  # noqa: BLE001 — no groups is the fail-closed default
        log.warning("chat: group lookup failed for a web turn", exc_info=True)
        groups = set()
    return access.Identity.make(user=email, groups=groups, surface="web")


class RunManager:
    """The replies this bridge is writing."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._runs: dict[str, Run] = {}
        self._claimed: set[str] = set()

    # -- lookup ------------------------------------------------------------------
    def active(self, message_id: str) -> Run | None:
        run = self._runs.get(message_id)
        return run if run is not None and not run.finished else None

    def active_in(self, conversation_id: str) -> Run | None:
        return next((r for r in list(self._runs.values())
                     if r.conversation_id == conversation_id and not r.finished), None)

    def claim(self, conversation_id: str) -> bool:
        """Reserve the conversation for one new reply (no await between the
        check and the reservation). False when a reply is already being written."""
        if conversation_id in self._claimed or self.active_in(conversation_id) is not None:
            return False
        self._claimed.add(conversation_id)
        return True

    def release(self, conversation_id: str) -> None:
        self._claimed.discard(conversation_id)

    # -- start -------------------------------------------------------------------
    def start(self, *, conversation_id: str, message_id: str, prompt: str, user,
              title: str | None = None) -> Run:
        """Begin writing ``message_id`` (already stored as 'running'). Must be
        called on the event loop, with the conversation claimed."""
        builder = MessageBuilder(message_id, conversation_id,
                                 show_tools=getattr(self.ctx.settings, "show_tools", "compact") != "off")
        run = Run(message_id=message_id, conversation_id=conversation_id, owner_id=user.id,
                  email=user.email, builder=builder, loop=asyncio.get_running_loop(),
                  started=time.monotonic())
        self._runs[message_id] = run
        self._publish(run, builder.start())
        if title:
            self._publish(run, builder.title(title))
        run.task = asyncio.create_task(self._execute(run, prompt), name=f"hubzoid-reply-{message_id}")
        return run

    # -- subscribers -------------------------------------------------------------
    def subscribe(self, run: Run) -> asyncio.Queue:
        """A queue of every chunk of the run so far and to come, then an end mark."""
        queue: asyncio.Queue = asyncio.Queue()
        for chunk in run.history:
            queue.put_nowait(chunk)
        if run.finished:
            queue.put_nowait(_END)
        else:
            run.subscribers.add(queue)
        return queue

    def unsubscribe(self, run: Run, queue: asyncio.Queue) -> None:
        run.subscribers.discard(queue)

    async def sse(self, run: Run, queue: asyncio.Queue | None = None) -> AsyncIterator[bytes]:
        """The run as an SSE body: its chunks, keep-alive comments while idle,
        then ``data: [DONE]``. Closing it only unsubscribes."""
        queue = queue if queue is not None else self.subscribe(run)
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield KEEPALIVE
                    continue
                if chunk is _END:
                    break
                yield encode(chunk)
            yield DONE
        finally:
            self.unsubscribe(run, queue)

    def _publish(self, run: Run, chunks: list[dict]) -> None:
        for chunk in chunks:
            run.history.append(chunk)
            for queue in list(run.subscribers):
                queue.put_nowait(chunk)

    def title_ready(self, conversation_id: str, title: str) -> None:
        """A generated title (called from any thread): tell the live reply's
        subscribers, if the reply is still being written."""
        for run in list(self._runs.values()):
            if run.conversation_id != conversation_id or run.closed:
                continue
            try:
                run.loop.call_soon_threadsafe(self._publish_title, run, title)
            except RuntimeError:  # the loop is closed; nobody is listening
                pass

    def _publish_title(self, run: Run, title: str) -> None:
        # Until 'finish' goes out (a reply being saved still takes a title).
        if not run.closed:
            self._publish(run, run.builder.title(title))

    # -- cancel ------------------------------------------------------------------
    async def cancel(self, message_id: str) -> bool:
        """Stop a reply being written here. False when there is none."""
        run = self.active(message_id)
        if run is None or run.task is None:
            return False
        try:
            same_loop = asyncio.get_running_loop() is run.loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            run.task.cancel()
        else:
            run.loop.call_soon_threadsafe(run.task.cancel)
        return True

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Stop every reply (the bridge is stopping) and wait for them to be saved."""
        running = [r for r in self._runs.values() if not r.finished and r.task is not None]
        for run in running:
            run.task.cancel()
        if running:
            await asyncio.wait([r.task for r in running], timeout=timeout)

    # -- the run -----------------------------------------------------------------
    async def _execute(self, run: Run, prompt: str) -> None:
        ctx = self.ctx
        status = "complete"
        usage: dict = {}
        identity = None
        if ctx.inflight is not None:
            ctx.inflight.enter()
        try:
            identity = await asyncio.to_thread(web_identity, ctx.hub_dir, run.email)
            with _request_ctx.chat_scope(run.conversation_id), access.identity_scope(identity):
                stream = run_events.stream_items(ctx.runtime, prompt)
                try:
                    async for item in stream:
                        chunks = run.builder.feed(item)
                        if chunks:
                            self._publish(run, chunks)
                        if isinstance(item, Notice) and item.kind == "error":
                            status = "error"
                        await self._checkpoint(run, force=isinstance(item, (ToolCall, ToolResult)))
                finally:
                    await run_events.aclose(stream)
                    usage = _request_ctx.drain_usage()
        except asyncio.CancelledError:
            status = "cancelled"
            task = asyncio.current_task()
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()
        except Exception as exc:  # noqa: BLE001 — the reply fails, the bridge does not
            log.exception("chat: reply %s failed", run.message_id)
            status = "error"
            self._publish(run, run.builder.fail(f"{type(exc).__name__}: {exc}"))
        finally:
            if ctx.inflight is not None:
                ctx.inflight.leave()
            await self._finish(run, status, usage, identity)

    async def _checkpoint(self, run: Run, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - run.last_persist < PERSIST_INTERVAL:
            return
        run.last_persist = now
        try:
            await asyncio.to_thread(self.ctx.store.update_message, run.message_id,
                                    content=run.builder.snapshot(), text=run.builder.plain_text())
        except Exception:  # noqa: BLE001 — the final save retries; the reply goes on
            log.warning("chat: could not save progress of %s", run.message_id, exc_info=True)

    async def _finish(self, run: Run, status: str, usage: dict, identity) -> None:
        chunks = run.builder.finish(status)
        run.status = status
        duration_ms = int((time.monotonic() - run.started) * 1000)
        error = run.builder.error if status == "error" else None
        summary = {k: usage.get(k) for k in ("input_tokens", "output_tokens", "cost_usd", "model")
                   if usage.get(k) is not None}
        summary["duration_ms"] = duration_ms
        try:
            # Save (and count) before the stream ends, so a client that reloads
            # on 'finish' reads the final message.
            try:
                await asyncio.to_thread(self._save_final, run, status, error,
                                        usage.get("model"), summary)
            except Exception:  # noqa: BLE001
                log.exception("chat: could not save the end of %s", run.message_id)
            await self._record_usage(run, status, usage, identity)
        finally:
            self._publish(run, chunks)
            run.closed = True
            for queue in list(run.subscribers):
                queue.put_nowait(_END)
            run.subscribers.clear()
            self._runs.pop(run.message_id, None)
            self._claimed.discard(run.conversation_id)
            run.done.set()

    def _save_final(self, run: Run, status: str, error: str | None, model: str | None,
                    summary: dict) -> None:
        store = self.ctx.store
        store.update_message(run.message_id, content=run.builder.snapshot(),
                             text=run.builder.plain_text(), status=status, error=error,
                             model=model, usage=summary)
        store.touch(run.conversation_id)

    async def _record_usage(self, run: Run, status: str, usage: dict, identity) -> None:
        from .. import server

        raw = dict(usage)
        if status == "cancelled":
            raw["status"] = "cancelled"
        elif status == "error":
            raw["status"] = "error"
        if identity is None:
            identity = access.Identity.make(user=run.email, surface="web")
        try:
            await server._record_turn(self.ctx.hub_dir, identity, run.conversation_id, raw, run.started)
        except Exception:  # noqa: BLE001 — usage is telemetry
            log.warning("chat: usage row for %s not recorded", run.message_id, exc_info=True)

    # -- restart -----------------------------------------------------------------
    def sweep(self) -> int:
        """Mark this hub's replies left 'running' by a previous process as failed."""
        try:
            count = self.ctx.store.sweep_running(self.ctx.hub, error=INTERRUPTED)
        except Exception:  # noqa: BLE001 — never block the bridge start
            log.warning("chat: could not check for interrupted replies", exc_info=True)
            return 0
        if count:
            log.info("chat: %d interrupted repl%s marked failed", count, "y" if count == 1 else "ies")
        return count
