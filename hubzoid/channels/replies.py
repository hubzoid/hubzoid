"""Posting to a thread's origin: rendered, split, and recorded.

Every part of every reply is one recorded outside write
(``hubzoid.work_items.operations``): the exact intent is saved before anything
is sent, the outcome after. A post whose outcome is unknown becomes
``ambiguous`` and is never sent again on its own. When the provider can look
for a part by its marker, an interrupted post is checked first, and only a
positive find settles it. An empty listing is never proof that nothing was
posted.

The destination is always the stored thread origin. Callers pass a key, never
a channel, thread or recipient.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from .config import Connection
from .errors import ChannelError
from .models import Origin, Outbound, Receipt

log = logging.getLogger("hubzoid.channels.replies")

#: what does not fit in the parts a provider allows goes behind a link
OVERFLOW_NOTE = "\n\n(The full reply is longer than this channel allows. Open it in Hubzoid.)"


def marker(thread_id: str, key: str, part: int) -> str:
    """An opaque, stable marker for one part, safe to show in a provider."""
    digest = hashlib.sha256(f"{thread_id}|{key}|{part}".encode()).hexdigest()[:16]
    return f"hz-{digest}"


def split(text: str, limit: int, max_parts: int) -> list[str]:
    """Split on paragraph boundaries, then lines, then Unicode code points,
    keeping fenced code blocks balanced. Never more than `max_parts` parts:
    the rest is cut with a note, so nothing is silently dropped."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    room = max(200, limit - 40)
    parts: list[str] = []
    current = ""
    for block in re.split(r"(\n\n+)", text):
        if not block:
            continue
        if len(current) + len(block) <= room:
            current += block
            continue
        if current.strip():
            parts.append(current)
        current = ""
        while len(block) > room:
            cut = block.rfind("\n", 0, room)
            cut = cut if cut > room // 2 else room
            parts.append(block[:cut])
            block = block[cut:]
        current = block
    if current.strip():
        parts.append(current)
    parts = _balance_fences([p.strip("\n") for p in parts if p.strip()])
    if len(parts) > max_parts:
        kept = parts[:max_parts]
        kept[-1] = kept[-1][: max(0, room - len(OVERFLOW_NOTE))] + OVERFLOW_NOTE
        parts = kept
    total = len(parts)
    if total > 1:
        parts = [f"({n}/{total})\n{p}" for n, p in enumerate(parts, 1)]
    return parts


def _balance_fences(parts: list[str]) -> list[str]:
    """A part that ends inside a ``` block is closed, and the next part
    reopens it, so every part renders on its own."""
    out, inside = [], False
    for p in parts:
        if inside:
            p = "```\n" + p
        if p.count("```") % 2 == 1:
            p += "\n```"
            inside = True
        else:
            inside = False
        out.append(p)
    return out


def post(conn: Connection, thread: dict, *, kind: str, text: str, key: str,
         blocks=None, ref: str | None = None, actions=None, draft=None) -> list[dict]:
    """Post `text` to the thread's origin under `key`. Returns one result per
    part: `{"state": "succeeded" | "ambiguous" | "failed" | "waiting", ...}`.
    Repeating the same key and text is safe: parts already sent are not sent
    again. The same key with different text is refused
    (`operation_argument_conflict`).

    Called inside a workflow run (``hub.thread.post``), the post belongs to
    that run's work item: `actions` name what it does (default
    ``channel.reply``; a customer-visible reply names ``customer_reply.send`` or
    ``email.send``), the hub's write modes apply, and a post that must wait for
    a person parks the work item instead of returning."""
    adapter = conn.adapter
    origin = Origin.from_json(thread["origin_json"])
    pieces = split(adapter.render(text), adapter.part_chars, adapter.max_parts)
    results = []
    for n, piece in enumerate(pieces, 1):
        mark = marker(thread["id"], key, n)
        out = Outbound(kind=kind, text=piece, part=n, parts=len(pieces), marker=mark,
                       blocks=blocks if n == len(pieces) else None)
        results.append(_post_part(conn, thread, origin, out, key, ref, actions, draft))
        if results[-1]["state"] != "succeeded":
            break   # later parts wait: a gap in the middle would mislead
    return results


def _in_workflow() -> bool:
    from ..workflows import context

    try:
        ctx = context._ctx()
    except Exception:  # noqa: BLE001 - no run bound
        return False
    return bool((ctx.work or {}).get("item"))


def _post_part(conn: Connection, thread: dict, origin: Origin, out: Outbound, key: str,
               ref: str | None = None, actions=None, draft=None) -> dict:
    from contextlib import nullcontext

    from ..work_items import operations as ops
    from ..work_items.errors import NotSent, Parked, WorkItemError
    from ..workflows.context import run_scope

    adapter = conn.adapter

    def dispatch(args: dict) -> dict:
        receipt = adapter.reply(conn, origin, out)
        return {"message_id": receipt.message_id, **(receipt.data or {})}

    def save_draft(args: dict) -> dict:
        receipt = draft(conn, origin, out)
        return {"draft_id": receipt.message_id, **(receipt.data or {})}

    def reconcile(args: dict):
        found = adapter.find(conn, origin, out.marker)
        if isinstance(found, Receipt):
            return ops.Reconciled(ops.APPLIED, result={"message_id": found.message_id, **(found.data or {})},
                                  evidence={"found_marker": out.marker})
        # Not found (False) or cannot look (None): never proof it was not posted.
        return ops.Reconciled(ops.UNKNOWN, evidence={"looked": found is not None})

    recovery = "reconcile" if adapter.recovery == "reconcile" else "escalate"
    arguments = {"kind": out.kind, "text": out.text, "part": out.part, "parts": out.parts,
                 "marker": out.marker, "key": key, "ref": ref}
    target = {"channel": conn.id, "thread": thread["id"], **({"ref": ref} if ref else {})}
    inside = _in_workflow()
    kwargs = dict(name=f"channel.{out.kind}", target=target, arguments=arguments, recovery=recovery,
                  dispatch=dispatch, reconcile=reconcile if recovery == "reconcile" else None,
                  key=f"{key}:{out.part}", channel=conn.id,
                  draft=save_draft if draft is not None else None)
    if inside:
        # The workflow's own work item: its route, write modes and parking apply.
        result = ops.run(actions=actions or "channel.reply", **kwargs)
        return {"state": "succeeded", "part": out.part, "result": result}
    scope = run_scope(hub=conn.hub, workflow=f"channel:{conn.id}", hub_dir=conn.hub_dir, engine=None,
                      subject=f"channel:{conn.id}", event={"id": f"thread:{thread['id']}"})
    with scope if not inside else nullcontext():
        try:
            result = ops.run(actions=actions, **kwargs)
            return {"state": "succeeded", "part": out.part, "result": result}
        except ops.OperationFailed as exc:
            state = "ambiguous" if exc.code == "operation_ambiguous" else "failed"
            log.warning("channel %s: reply %s part %s is %s", conn.id, key, out.part, state)
            return {"state": state, "part": out.part, "code": exc.code, "operation_id": exc.operation_id}
        except NotSent as exc:
            return {"state": "failed", "part": out.part, "code": getattr(exc, "code", "not_sent"),
                    "message": str(exc)}
        except Parked as exc:
            return {"state": "waiting", "part": out.part, "code": exc.kind, "operation_id": exc.operation_id}
        except (WorkItemError, ChannelError) as exc:
            return {"state": "failed", "part": out.part, "code": exc.code, "message": exc.message}


def delivered(results: list[dict]) -> bool:
    return bool(results) and all(r["state"] == "succeeded" for r in results)


def outbox(hub_dir, hub: str, cid: str, *, state: str | None = None, thread: str | None = None,
           limit: int = 50) -> list[dict]:
    """The replies recorded for a connection, newest first, from the operation
    ledger. Payload text is not returned."""
    from sqlalchemy import text as sql

    from ..work_items import store as wstore

    where = "hub=:h AND name LIKE 'channel.%' AND target_json LIKE :w"
    params = {"h": hub, "w": f'%"channel":"{cid}"%', "n": max(1, min(int(limit), 200))}
    if state:
        where += " AND state=:s"
        params["s"] = state
    with wstore.engine(Path(hub_dir)).connect() as c:
        rows = c.execute(sql(f"SELECT id,name,state,target_json,attempts_json,error,result_json,created,updated,"
                             f"revision FROM hz_operations WHERE {where} ORDER BY created DESC LIMIT :n"),
                         params).mappings().all()
    out = []
    import json as _json

    for r in rows:
        target = _json.loads(r["target_json"] or "{}")
        if thread and target.get("thread") != thread:
            continue
        out.append({"id": r["id"], "kind": r["name"].removeprefix("channel."), "state": r["state"],
                    "thread_id": target.get("thread"), "error": r["error"],
                    "receipt": _json.loads(r["result_json"]) if r["result_json"] else None,
                    "attempts": len(_json.loads(r["attempts_json"] or "[]")), "created": r["created"],
                    "updated": r["updated"], "revision": r["revision"]})
    return out


def resolve(conn: Connection, op_id: str, *, decision: str, actor: str, evidence=None,
            acknowledge_duplicate_risk: bool = False) -> dict:
    """An administrator settles an uncertain reply: `check` asks the provider
    for the part's marker (read-only), `done` records evidence that it was
    posted, `retry` posts it again accepting a possible duplicate, `abandon`
    gives up. Only a positive find or a person's evidence settles it as
    posted."""
    from ..work_items import operations as ops
    from ..work_items import service as wi
    from ..work_items import store as wstore

    row = wstore.get_op(conn.hub_dir, op_id)
    target = (row or {}).get("target_json") or {}
    if row is None or row["hub"] != conn.hub or target.get("channel") != conn.id or \
            not str(row.get("name", "")).startswith("channel."):
        raise ChannelError("not_found", "No reply with that ID on this channel.", status=404)
    args = ops.row_arguments(conn.hub_dir, row)
    thread = __import__("hubzoid.channels.store", fromlist=["x"]).get_thread(conn.hub_dir, target.get("thread"))
    origin = Origin.from_json(thread["origin_json"])
    # Replies outside a work item are requested by the connection itself, and
    # the caller already holds channels.manage. The person is in the audit log.
    who = row.get("requester") or f"channel:{conn.id}"
    if decision == "check":
        if row["state"] not in ("ambiguous", "sending"):
            return {"id": op_id, "state": row["state"], "found": None}
        found = conn.adapter.find(conn, origin, args.get("marker") or "")
        if isinstance(found, Receipt):
            if row["state"] == "ambiguous":
                wi.resolve(conn.hub_dir, op_id, who, decision="done",
                           evidence={"found_marker": args.get("marker"), "message_id": found.message_id})
            return {"id": op_id, "state": "resolved_done", "found": True}
        return {"id": op_id, "state": row["state"], "found": False if found is False else None,
                "message": "Not found. That does not prove it was not posted, so it stays uncertain."}
    out = wi.resolve(conn.hub_dir, op_id, who, decision=decision, evidence=evidence,
                     acknowledge_duplicate_risk=acknowledge_duplicate_risk)
    if decision == "retry":
        part = Outbound(kind=args["kind"], text=args["text"], part=int(args["part"]), parts=int(args["parts"]),
                        marker=args["marker"])
        result = _post_part(conn, thread, origin, part, args["key"], args.get("ref"))
        return {"id": op_id, "state": result["state"], "retried": True}
    return {"id": op_id, "state": out.get("state") if isinstance(out, dict) else decision}
