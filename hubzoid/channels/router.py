"""Routing: from an accepted delivery to work, and from finished work back to
the thread.

Runs in the bridge, after admission committed. Each accepted delivery is
claimed (``accepted`` to ``routing``), checked and handed to exactly one place:

* **Agent connections** go to the native chat engine. Each person in a thread
  has their own conversation (owned by their linked account, so every run uses
  their own access), and the engine's queue and idempotency keys give one reply
  at a time and absorb repeats. A thread has one active person at a time: while
  someone else's reply is running, a delivery waits.
* **Workflow connections** start a durable work item keyed by the triggering
  event, or pass a follow-up to the thread's active item.

Finished work comes back the same way, driven by the deliveries themselves:
a routed delivery stays "owed a reply" until the run or work item it started
has finished and its answer is posted. Only runs a channel delivery started
are posted, so a person's own branch in the chat app stays private. Each
post is a recorded outside write (``replies``) under a key derived from the
run or work item, so the pass can repeat after a crash or a rate limit and
posts each part once. It stops at a part whose outcome is unknown.

One person's request is active in a thread at a time, across workers: a
claim on the thread row, taken in one statement and released when their
request finishes.

The identity rules: a person must have an account link set by an
administrator, an active account with access to this hub, and, when the
connection lists groups, one of them. Anyone else gets one neutral notice and
no model call.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from . import admission, replies, store
from . import config as config_lib
from .errors import ChannelError
from .models import InboundEvent

log = logging.getLogger("hubzoid.channels.router")

#: a claimed delivery older than this is taken to be from a worker that died
STALE_ROUTING_SECONDS = 120
#: how often the loop wakes without a kick
TICK_SECONDS = 5.0

NOTICES = {
    "identity_unmapped": ("I can't act for you here yet. Ask a Hubzoid administrator to link your "
                          "{provider} account to your Hubzoid account."),
    "account_inactive": "I can't act for you: your Hubzoid account is not active. Ask an administrator.",
    "hub_access_denied": "I can't act for you: your Hubzoid account does not have access to this hub.",
    "group_not_allowed": "I can't act for you: this channel is limited to certain groups. Ask an administrator.",
    "terms_required": "Please open Hubzoid and accept the current terms of use first, then ask again.",
    "empty_request": "Tell me what you want done, or attach a file.",
    "queue_full": "There are too many requests waiting in this thread. Try again once they finish.",
    "workflow_denied": "I can't start this work for you: you do not have permission to run it.",
}


# -- the loop -------------------------------------------------------------------------

class _Loop:
    def __init__(self):
        self.event: asyncio.Event | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.app = None
        self.hub_dir: Path | None = None
        self.transports: dict[str, object] = {}
        self.results: list[dict] = []
        self.lock = threading.Lock()


_state = _Loop()


def kick(hub_dir=None) -> None:
    """Wake the router (thread-safe). A no-op where no router runs (the CLI)."""
    loop, event = _state.loop, _state.event
    if loop is None or event is None:
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        pass


async def start(app, hub_dir) -> None:
    """Start routing for this bridge: the loop and each enabled connection's
    long-lived transport. Called from the bridge lifespan."""
    _state.app, _state.hub_dir = app, Path(hub_dir)
    _state.loop = asyncio.get_running_loop()
    _state.event = asyncio.Event()
    chat = getattr(app.state, "chat", None)
    if chat is not None:
        previous = chat.engine.on_terminal

        def on_terminal(**kw):
            if previous is not None:
                try:
                    previous(**kw)
                except Exception:  # noqa: BLE001 - another feature's hook never blocks ours
                    log.warning("channels: an earlier run-end hook failed", exc_info=True)
            kick()

        chat.engine.on_terminal = on_terminal
    await reload_transports()
    _state.task = asyncio.create_task(_run())
    _state.event.set()


async def stop() -> None:
    task, _state.task = _state.task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    for name, handle in list(_state.transports.items()):
        try:
            await asyncio.to_thread(handle.stop)
        except Exception:  # noqa: BLE001
            log.warning("channels: could not stop %s cleanly", name, exc_info=True)
    _state.transports.clear()
    _state.loop = _state.event = None


async def reload_transports() -> None:
    """Start transports for enabled connections and stop those no longer
    enabled (after an apply)."""
    hub_dir = _state.hub_dir
    if hub_dir is None:
        return
    try:
        conns = await asyncio.to_thread(config_lib.active_all, hub_dir)
    except Exception:  # noqa: BLE001
        log.warning("channels: could not read connections", exc_info=True)
        return
    want = {f"{c.id}@{c.revision}": c for c in conns if c.enabled}
    for name in list(_state.transports):
        if name not in want:
            handle = _state.transports.pop(name)
            try:
                await asyncio.to_thread(handle.stop)
            except Exception:  # noqa: BLE001
                log.warning("channels: could not stop %s", name, exc_info=True)
    for name, conn in want.items():
        if name in _state.transports:
            continue
        try:
            handle = await asyncio.to_thread(conn.adapter.start, conn, hub_dir)
        except Exception:  # noqa: BLE001 - one broken transport never stops the rest
            log.exception("channels: could not start %s", name)
            continue
        if handle is not None:
            _state.transports[name] = handle


async def _run() -> None:
    while True:
        try:
            await asyncio.wait_for(_state.event.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
        _state.event.clear()
        try:
            await asyncio.to_thread(_recover_stale, _state.hub_dir)
            await route_pending(_state.hub_dir, _state.app)
            if await deliver_results(_state.hub_dir, _state.app):
                kick()    # a finished request may free a waiting thread
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop keeps going
            log.exception("channels: routing pass failed")


# -- routing ---------------------------------------------------------------------------

async def route_pending(hub_dir, app=None) -> int:
    """Route what is accepted or waiting, oldest first, in thread order. Returns
    how many deliveries moved on."""
    hub_dir = Path(hub_dir)
    hub = config_lib.hub_of(hub_dir)
    rows = await asyncio.to_thread(store.open_deliveries, hub_dir, hub)
    blocked_threads: set[str] = set()
    moved = 0
    for row in rows:
        if row["thread_id"] in blocked_threads:
            continue
        outcome, plan = await asyncio.to_thread(_route_one, hub_dir, row, app)
        if plan is not None and app is not None:
            await app.state.chat.orchestrator.launch(plan)
        if outcome == "waiting":
            blocked_threads.add(row["thread_id"])
        else:
            moved += 1
    return moved


def _claim(hub_dir, row: dict) -> dict | None:
    return store.update_delivery(hub_dir, row["id"], expect_status=("accepted", "waiting"), status="routing")


def _finish(hub_dir, did: str, status: str, reason: str | None = None, **route) -> None:
    fields = {"status": status, "reason": reason}
    if route:
        current = store.get_delivery(hub_dir, did) or {}
        fields["route_json"] = {**(current.get("route_json") or {}), **route}
    store.update_delivery(hub_dir, did, **fields)


def _route_one(hub_dir: Path, row: dict, app) -> tuple[str, object]:
    claimed = _claim(hub_dir, row)
    if claimed is None:
        return "skipped", None
    try:
        conn = config_lib.active(hub_dir, row["connection_id"])
        thread = store.get_thread(hub_dir, row["thread_id"])
        event = admission.event_of(hub_dir, row)
        if not conn.enabled:
            _finish(hub_dir, row["id"], "disabled", "connection_disabled")
            return "disabled", None
        if event.trigger and not event.text.strip() and not event.files:
            _notice(conn, thread, event, "empty_request")
            _finish(hub_dir, row["id"], "ignored", "empty_request")
            return "ignored", None
        if conn.config.agent:
            return _route_chat(hub_dir, conn, row, thread, event, app)
        return _route_workflow(hub_dir, conn, row, thread, event)
    except ChannelError as exc:
        log.warning("channel %s: delivery %s not routed: %s", row["connection_id"], row["id"], exc.code)
        _finish(hub_dir, row["id"], "failed", exc.code)
        return "failed", None
    except Exception:  # noqa: BLE001 - back to accepted for the next pass
        log.exception("channel %s: routing delivery %s failed", row["connection_id"], row["id"])
        store.update_delivery(hub_dir, row["id"], expect_status="routing", status="accepted")
        return "error", None


# -- identity ----------------------------------------------------------------------------

def person_for(conn: config_lib.Connection, event: InboundEvent):
    """The linked, active hub account behind an event, or (None, reason)."""
    from ..auth import users

    if event.actor is None:
        return None, "identity_unmapped"
    link = store.resolve_link(conn.hub_dir, conn.hub, conn.provider, event.origin.workspace_id,
                              event.actor.external_id)
    if link is None:
        return None, "identity_unmapped"
    user = users.get(conn.hub_dir, link["account_id"])
    if user is None or (user.get("status") or "active") != "active":
        return None, "account_inactive"
    return SimpleNamespace(id=user["id"], email=(user.get("email") or link["account_email"]).lower(),
                           name=user.get("name") or ""), None


def _groups_ok(conn: config_lib.Connection, email: str) -> bool:
    if not conn.config.allowed_groups:
        return False
    from ..access.groups import effective_groups

    try:
        groups = effective_groups(conn.hub_dir, email=email, surface="channel")
    except Exception:  # noqa: BLE001 - fail closed
        return False
    return bool(set(conn.config.allowed_groups) & set(groups))


def _hub_access(hub_dir: Path, email: str) -> str | None:
    from fastapi import HTTPException

    from ..chat.common import check_use_hub

    try:
        check_use_hub(hub_dir, email)
    except HTTPException as exc:
        return "hub_access_denied" if exc.status_code == 403 else "unavailable"
    return None


def _notice(conn, thread: dict, event: InboundEvent, reason: str) -> None:
    template = NOTICES.get(reason)
    if not template or thread is None:
        return
    who = event.actor.external_id if event.actor else "unknown"
    try:
        replies.post(conn, thread, kind="notice", text=template.format(provider=conn.adapter.title or conn.provider),
                     key=f"notice:{reason}:{who}")
    except Exception:  # noqa: BLE001 - a notice that cannot be posted is only logged
        log.warning("channel %s: could not post a %s notice", conn.id, reason, exc_info=True)


def _deny(hub_dir, conn, row, thread, event, reason: str) -> tuple[str, None]:
    _notice(conn, thread, event, reason)
    _finish(hub_dir, row["id"], "denied", reason)
    return "denied", None


# -- agent connections ----------------------------------------------------------------------

def _route_chat(hub_dir: Path, conn, row: dict, thread: dict, event: InboundEvent, app):
    person, reason = person_for(conn, event)
    if person is None:
        return _deny(hub_dir, conn, row, thread, event, reason)
    if not _groups_ok(conn, person.email):
        return _deny(hub_dir, conn, row, thread, event, "group_not_allowed")
    denied = _hub_access(hub_dir, person.email)
    if denied == "hub_access_denied":
        return _deny(hub_dir, conn, row, thread, event, denied)
    if denied or app is None or getattr(app.state, "chat", None) is None:
        # No chat engine in this process (or the access check is down): keep it for the bridge.
        store.update_delivery(hub_dir, row["id"], expect_status="routing", status="accepted")
        return "waiting", None
    chat = app.state.chat
    engine = chat.engine
    actor = engine.actor(person)
    try:
        if engine.admission is not None:
            engine.admission(actor)
    except Exception as exc:  # noqa: BLE001 - GraphError from the engine's own check
        code = getattr(exc, "code", "hub_access_denied")
        return _deny(hub_dir, conn, row, thread, event,
                     "terms_required" if code == "terms_required" else "hub_access_denied")
    convs = dict(thread.get("conversations_json") or {})
    mine = convs.get(person.id)
    others_busy = any(account != person.id and conversation_busy(engine, cid) for account, cid in convs.items())
    if others_busy or not store.claim_thread(hub_dir, thread["id"], person.email):
        store.update_delivery(hub_dir, row["id"], expect_status="routing", status="waiting", reason="thread_busy")
        return "waiting", None
    first = mine is None
    if first:
        mine = _open_conversation(hub_dir, conn, thread, actor, chat)
    content = _content(conn, event, person, chat, mine, first=first)
    branch = thread_branch(engine, mine, actor.id)
    out, plan = engine.enqueue(mine, actor, {"content": content, "lane": "queue", "branch_id": branch},
                               key=f"channel:{row['id']}")
    body = out.body if isinstance(out.body, dict) else {}
    _finish(hub_dir, row["id"], "routed", None, target="chat", conversation_id=mine, account=person.email,
            dispatched=bool(body.get("dispatched")), run_id=body.get("run_id"),
            queue_item=(body.get("item") or {}).get("id"))
    if event.trigger and conn.config.subscriptions == "on_accept" and not thread.get("subscribed"):
        store.update_thread(hub_dir, thread["id"], subscribed=1, requester=thread.get("requester") or person.email)
    if not out.replayed:
        _acknowledge(conn, thread, event, row)
    return "routed", (plan if not out.replayed else None)


def conversation_busy(engine, conversation_id: str) -> bool:
    """A reply is running, or a message is queued to run, in this
    conversation. A paused or blocked queue does not hold the thread."""
    import sqlalchemy as sa

    from ..chat import graph as g

    with engine.engine.connect() as c:
        branch_ids = sa.select(g.branches.c.id).where(g.branches.c.conversation_id == conversation_id)
        lease = c.execute(sa.select(sa.func.count()).select_from(g.leases).where(
            g.leases.c.branch_id.in_(branch_ids), g.leases.c.expires_at > time.time())).scalar() or 0
        live = c.execute(sa.select(sa.func.count()).select_from(g.queue).where(
            g.queue.c.conversation_id == conversation_id,
            g.queue.c.state.in_(("queued", "dispatching")))).scalar() or 0
    return bool(lease or live)


def thread_branch(engine, conversation_id: str, actor_id: str) -> str:
    """The person's first line of work in the conversation: the thread's own
    branch. A branch they make later in the chat app stays theirs alone."""
    import sqlalchemy as sa

    from ..chat import graph as g

    with engine.engine.connect() as c:
        bid = c.execute(sa.select(g.branches.c.id).where(
            g.branches.c.conversation_id == conversation_id, g.branches.c.writer_actor_id == actor_id)
            .order_by(g.branches.c.created_at).limit(1)).scalar()
    if bid is None:
        raise ChannelError("unavailable", "The thread's conversation has no branch for this person.", status=503)
    return bid


def _open_conversation(hub_dir: Path, conn, thread: dict, actor, chat) -> str:
    made = chat.engine.create_conversation(actor, agent=chat.model_label,
                                           key=f"channel-thread:{thread['id']}:{actor.id}")
    cid = made["conversation_id"]
    title = conn.adapter.thread_title(thread) if hasattr(conn.adapter, "thread_title") else None
    import sqlalchemy as sa

    from ..chat.store import conversations

    with chat.engine.engine.begin() as c:
        c.execute(sa.update(conversations).where(conversations.c.id == cid, conversations.c.title.is_(None))
                  .values(title=title or f"{conn.adapter.title or conn.provider} thread", title_source="user"))
        c.execute(sa.update(conversations).where(conversations.c.id == cid).values(source="channel"))
    for _ in range(5):
        current = store.get_thread(hub_dir, thread["id"])
        convs = dict(current.get("conversations_json") or {})
        if actor.id in convs:
            return convs[actor.id]
        convs[actor.id] = cid
        try:
            store.update_thread(hub_dir, thread["id"], expected_revision=current["revision"],
                                conversations_json=convs)
            return cid
        except ChannelError:
            continue
    raise ChannelError("unavailable", "Could not bind the thread to a conversation.", status=503)


def _content(conn, event: InboundEvent, person, chat, conversation_id: str, *, first: bool = False) -> list[dict]:
    context = conn.adapter.context(conn, event, first=first)
    body = event.text.strip()
    parts: list[dict] = [{"type": "text", "text": f"{context}\n\n{body}".strip() if context else body}]
    notes = []
    for ref in event.files:
        try:
            file_id = _ingest_file(conn, event, ref, person, chat, conversation_id)
            parts.append({"type": "file", "file_id": file_id})
        except ChannelError as exc:
            notes.append(f"(The attachment {ref.get('name') or ''} was not added: {exc.message})")
    if notes:
        parts[0]["text"] += "\n\n" + "\n".join(notes)
    return parts


def _ingest_file(conn, event: InboundEvent, ref: dict, person, chat, conversation_id: str) -> str:
    fetch = getattr(conn.adapter, "fetch_file", None)
    files = getattr(chat, "files", None)
    if fetch is None or files is None:
        raise ChannelError("attachment_unsupported", "this channel does not bring files in", status=422)
    name, mime, data = fetch(conn, event, ref)
    rec = files.register_bytes(person.id, name, mime, data, origin="channel")
    file_id = rec["id"] if isinstance(rec, dict) else rec
    try:
        from ..chat.engine import Actor

        files.attach(Actor(id=person.id, email=person.email), file_id, conversation_id)
    except Exception:  # noqa: BLE001 - the part validator reports a file it cannot use
        log.warning("channel %s: could not attach %s", conn.id, name, exc_info=True)
    return file_id


def _acknowledge(conn, thread: dict, event: InboundEvent, row: dict) -> None:
    ack = getattr(conn.adapter, "acknowledge", None)
    try:
        if ack is not None:
            ack(conn, thread, event, row)
        elif event.trigger:
            replies.post(conn, thread, kind="ack", text="Accepted. Working on it.", key=f"ack:{row['id']}")
    except Exception:  # noqa: BLE001 - work goes on without the acknowledgement
        log.warning("channel %s: acknowledgement failed", conn.id, exc_info=True)


# -- workflow connections -------------------------------------------------------------------

def _route_workflow(hub_dir: Path, conn, row: dict, thread: dict, event: InboundEvent):
    from ..work_items import service as wi
    from ..work_items import store as wstore
    from ..work_items.errors import WorkItemError

    person, reason = person_for(conn, event)
    unattended = bool(event.data.get("unattended"))
    if person is None and not unattended:
        return _deny(hub_dir, conn, row, thread, event, reason)
    if person is not None and not unattended and not _groups_ok(conn, person.email):
        return _deny(hub_dir, conn, row, thread, event, "group_not_allowed")
    if person is not None and not unattended:
        denied = _hub_access(hub_dir, person.email)
        if denied == "hub_access_denied":
            return _deny(hub_dir, conn, row, thread, event, denied)
        if denied:
            store.update_delivery(hub_dir, row["id"], expect_status="routing", status="accepted")
            return "waiting", None
    requester = person.email if person is not None and not unattended else f"workflow:{conn.config.workflow}"
    active = thread.get("work_item_id")
    if active:
        item = wstore.get_item(hub_dir, active)
        if item is not None and item["state"] not in wstore.TERMINAL:
            if person is not None and item.get("requester") == person.email:
                wi.message(hub_dir, active, person.email, event.text or "(no text)",
                           client_message_id=row["id"])
                _finish(hub_dir, row["id"], "routed", None, target="work_item", work_item_id=active,
                        follow_up=True, replied="follow_up")
                return "routed", None
            store.update_delivery(hub_dir, row["id"], expect_status="routing", status="waiting",
                                  reason="thread_busy")
            return "waiting", None
    if not store.claim_thread(hub_dir, thread["id"], requester):
        store.update_delivery(hub_dir, row["id"], expect_status="routing", status="waiting", reason="thread_busy")
        return "waiting", None
    origin = {"surface": "channel", "channel": conn.id, "provider": conn.provider, "thread_id": thread["id"],
              "acts_as": requester,
              "requested_by": event.actor.to_json() if event.actor else None}
    payload = {"channel": conn.id, "thread_id": thread["id"], "delivery_id": row["id"], "kind": event.kind,
               "text": event.text, "files": event.files, "data": event.data,
               "actor": event.actor.to_json() if event.actor else None, "origin": event.origin.to_json()}
    try:
        item, created = wi.admit_and_start(
            hub_dir, workflow=conn.config.workflow, key=f"channel:{conn.id}:{event.semantic_key}",
            route=event.data.get("route"), input=payload, requester=requester, origin=origin,
            actor=person.email if (person is not None and not unattended) else None)
    except WorkItemError as exc:
        if exc.status == 403:
            return _deny(hub_dir, conn, row, thread, event, "workflow_denied")
        if exc.code in ("backlog_full", "admission_paused"):
            store.update_delivery(hub_dir, row["id"], expect_status="routing", status="accepted")
            return "waiting", None
        raise ChannelError(exc.code, exc.message, status=exc.status) from exc
    store.update_thread(hub_dir, thread["id"], work_item_id=item["id"],
                        subscribed=1 if conn.config.subscriptions == "on_accept" else thread.get("subscribed", 0),
                        requester=thread.get("requester") or requester)
    _finish(hub_dir, row["id"], "routed", None, target="work_item", work_item_id=item["id"], account=requester)
    if created:
        _acknowledge(conn, thread, event, row)
    return "routed", None


# -- results back to the origin ---------------------------------------------------------------

#: how long a routed delivery is watched for its reply
REPLY_WINDOW_SECONDS = 7 * 24 * 3600


async def deliver_results(hub_dir, app=None) -> int:
    """Post the replies routed deliveries still owe. Returns how many
    deliveries finished (posted, or settled as failed or uncertain)."""
    hub_dir = Path(hub_dir)
    rows = await asyncio.to_thread(store.unreplied, hub_dir, config_lib.hub_of(hub_dir),
                                   since=time.time() - REPLY_WINDOW_SECONDS)
    done = 0
    for row in rows:
        try:
            if await asyncio.to_thread(_reply_for, hub_dir, app, row):
                done += 1
        except Exception:  # noqa: BLE001 - the next pass tries again
            log.exception("channels: posting the reply for delivery %s failed", row["id"])
    return done


def _settle_reply(hub_dir: Path, row: dict, outcome: str, thread: dict | None, account: str | None) -> bool:
    _finish(hub_dir, row["id"], "routed", None, replied=outcome)
    if thread is not None and account:
        store.release_thread(hub_dir, thread["id"], account)
    return True


def _outcome(results: list[dict]) -> str | None:
    """`posted`, `failed` or `uncertain` once the reply is settled; None to try
    again on the next pass (a rate limit, a refusal before sending)."""
    if not results:
        return "posted"
    last = results[-1]
    if replies.delivered(results):
        return "posted"
    if last["state"] == "ambiguous":
        return "uncertain"
    if last["state"] == "failed" and last.get("code") in ("not_sent", "operation_failed_before_send"):
        return None
    if last["state"] == "waiting":
        return None
    return "failed"


def _run_of(engine_db, row: dict) -> dict | None:
    """The chat run a delivery started: directly, or once its queued message
    was dispatched."""
    import sqlalchemy as sa

    from ..chat import graph as g

    route = row.get("route_json") or {}
    run_id = route.get("run_id")
    with engine_db.connect() as c:
        if not run_id and route.get("queue_item"):
            run_id = c.execute(sa.select(g.queue.c.dispatched_run_id).where(
                g.queue.c.id == route["queue_item"])).scalar()
        if not run_id:
            return None
        r = c.execute(sa.select(g.runs.c.id, g.runs.c.message_id, g.runs.c.status).where(
            g.runs.c.id == run_id)).first()
    return {"id": r[0], "message_id": r[1], "status": r[2]} if r else None


TERMINAL_RUNS = {"complete": "complete", "stopped": "stopped", "interrupted": "error", "failed": "error",
                 "error": "error"}


def _reply_for(hub_dir: Path, app, row: dict) -> bool:
    route = row.get("route_json") or {}
    thread = store.get_thread(hub_dir, row["thread_id"]) if row.get("thread_id") else None
    if thread is None:
        return _settle_reply(hub_dir, row, "no_thread", None, None)
    try:
        conn = config_lib.active(hub_dir, thread["connection_id"])
    except ChannelError:
        return False
    if route.get("target") == "chat":
        return _chat_reply(hub_dir, app, conn, thread, row)
    if route.get("target") == "work_item":
        return _work_item_reply(hub_dir, conn, thread, row)
    return _settle_reply(hub_dir, row, "none", thread, route.get("account"))


def _chat_reply(hub_dir: Path, app, conn, thread: dict, row: dict) -> bool:
    import sqlalchemy as sa

    from ..chat.store import messages

    chat = getattr(getattr(app, "state", None), "chat", None) if app is not None else None
    db = chat.engine.engine if chat is not None else store.engine(hub_dir)
    run = _run_of(db, row)
    if run is None or run["status"] not in TERMINAL_RUNS:
        return False      # queued or still running
    with db.connect() as c:
        msg = c.execute(sa.select(messages.c.text, messages.c.status).where(messages.c.id == run["message_id"])).first()
    text_out = ((msg[0] if msg else "") or "").strip()
    state = TERMINAL_RUNS[run["status"]]
    ref = f"run:{run['id']}"
    if state == "complete" and text_out:
        results = replies.post(conn, thread, kind="result", text=text_out, key=f"result:{run['id']}", ref=ref)
    elif state == "stopped":
        results = replies.post(conn, thread, kind="notice", text="Stopped before finishing.",
                               key=f"result:{run['id']}", ref=ref)
    else:
        results = replies.post(conn, thread, kind="error",
                               text=f"I could not finish this request (reply {run['message_id'][-8:]}). Try again, "
                                    "or ask an administrator to check the run in Hubzoid.",
                               key=f"result:{run['id']}", ref=ref)
    outcome = _outcome(results)
    if outcome is None:
        return False
    account = (row.get("route_json") or {}).get("account")
    convs = thread.get("conversations_json") or {}
    busy = chat is not None and any(conversation_busy(chat.engine, cid) for cid in convs.values())
    return _settle_reply(hub_dir, row, outcome, thread if not busy else None, account)


def _work_item_reply(hub_dir: Path, conn, thread: dict, row: dict) -> bool:
    from ..work_items import store as wstore

    route = row.get("route_json") or {}
    item = wstore.get_item(hub_dir, route.get("work_item_id")) if route.get("work_item_id") else None
    if item is None:
        return _settle_reply(hub_dir, row, "no_item", thread, route.get("account"))
    if item["state"] not in wstore.TERMINAL:
        return False
    result = (item.get("result_json") or {}).get("result") if isinstance(item.get("result_json"), dict) else None
    body = _final_text(item["state"], result, item.get("error"), item["id"])
    if body is None:
        return _settle_reply(hub_dir, row, "posted_by_workflow", thread, route.get("account"))
    results = replies.post(conn, thread, kind="result" if item["state"] == "completed" else "error", text=body,
                           key=f"final:item:{item['id']}", ref=f"item:{item['id']}")
    outcome = _outcome(results)
    if outcome is None:
        return False
    return _settle_reply(hub_dir, row, outcome, thread, route.get("account"))


def _final_text(state: str, result, error: str | None, item_id: str) -> str | None:
    short = item_id[:8]
    if state == "completed":
        if isinstance(result, dict) and result.get("posted"):
            return None     # the workflow already answered in the thread
        if isinstance(result, str) and result.strip():
            return result.strip()
        if isinstance(result, dict):
            for key in ("reply", "summary", "message", "text"):
                if isinstance(result.get(key), str) and result[key].strip():
                    return result[key].strip()
        return f"Done (work {short})."
    if state == "cancelled":
        return f"Cancelled (work {short}). Anything already done is listed in Hubzoid."
    if state == "stopped":
        return f"Stopped (work {short}). It needs a person to decide in Hubzoid before it can continue."
    return f"This work could not finish (work {short}). An administrator can see why in Hubzoid."


# -- recovery ----------------------------------------------------------------------------------

def _recover_stale(hub_dir: Path) -> int:
    """Deliveries claimed by a worker that died go back to accepted."""
    cutoff = time.time() - STALE_ROUTING_SECONDS
    with store.engine(hub_dir).begin() as c:
        return c.execute(text("UPDATE hz_channel_deliveries SET status='accepted', updated=:t WHERE "
                              "status='routing' AND updated < :c"), {"t": time.time(), "c": cutoff}).rowcount
