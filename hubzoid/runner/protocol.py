"""The request and event format between the hub service and an AgentCore runner.

A request is one JSON object POSTed to ``/invocations``:

  {"v": 1, "kind": "hello" | "turn" | "agent", "call": "<work order id>",
   "prompt": "...", "context": {...}, "options": {...}}

``hello`` answers the runner's version and image without a model call.
``turn`` streams one agent turn as server-sent events. ``agent`` runs one turn
and answers its text only (``hub.call_agent``), also as events.

Every event is one SSE ``data:`` line holding a JSON object with ``type`` and
``seq``. Events map one to one onto ``hubzoid.run_events`` (``text`` for answer
text, the dataclass name for the rest), plus:

  start         the runner accepted the call (``fresh``: first call this microVM served)
  hitl_waiting  approval notes the gate recorded, sent before the tool result they belong to
  artifact      a file the turn recorded (``name``, ``url``)
  runlog        a scheduled task's run-log event, relayed to the hub service
  end           usage (drained, with the budget hold id), the session outputs
                (``session_id``, ``session_cost``, ``resumed``, ``runtime``),
                the run failure and the error, if any

A ``: keepalive`` comment line goes out every 15 seconds while nothing else does.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, Iterable, Iterator

VERSION = 1
KEEPALIVE_SECONDS = 15.0
#: The longest a streamed turn may run. AgentCore ends a stream at 60 minutes.
MAX_STREAM_SECONDS = 55 * 60
EVENT_TYPES = ("text", "ToolCall", "ToolResult", "ReasoningDelta", "ReasoningEnd", "Notice")


def encode_item(item: Any) -> dict | None:
    """A ``run_events`` stream item as a JSON-safe dict, or None for nothing."""
    if isinstance(item, str):
        return {"type": "text", "text": item} if item else None
    if dataclasses.is_dataclass(item) and type(item).__name__ in EVENT_TYPES:
        return {"type": type(item).__name__, **dataclasses.asdict(item)}
    return None


def decode_item(event: dict) -> Any:
    """The ``run_events`` item an encoded event stands for, or None."""
    from .. import run_events

    kind = event.get("type")
    if kind == "text":
        return str(event.get("text") or "")
    if kind not in EVENT_TYPES:
        return None
    cls = getattr(run_events, kind)
    fields = {f.name for f in dataclasses.fields(cls)}
    try:
        return cls(**{k: v for k, v in event.items() if k in fields})
    except TypeError:
        return None


def sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event, default=str, separators=(",", ":")).encode() + b"\n\n"


KEEPALIVE = b": keepalive\n\n"


def iter_sse(chunks: Iterable[bytes]) -> Iterator[dict]:
    """The events in a stream of raw bytes, as they complete. Comment lines
    (keep-alives) yield ``{"type": "keepalive"}`` so a reader can reset its timer."""
    buf = b""
    for chunk in chunks:
        if not chunk:
            continue
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            data = [line[5:].lstrip() for line in block.split(b"\n") if line.startswith(b"data:")]
            if data:
                try:
                    yield json.loads(b"\n".join(data))
                except ValueError:
                    continue
            elif block.startswith(b":"):
                yield {"type": "keepalive"}


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def digest(request: dict) -> str:
    """SHA-256 of a request without its call id: what the work order binds."""
    body = {k: v for k, v in request.items() if k != "call"}
    return hashlib.sha256(canonical(body)).hexdigest()


def session_id(hub: str, key: str) -> str:
    """The AgentCore session id for one chat (or one workflow run) of a hub:
    ``hz-<hub, 16 chars>-<40 hex>``, 33 to 100 characters of ``[a-zA-Z0-9-_]``,
    which AgentCore requires."""
    slug = "".join(c if c.isalnum() else "-" for c in (hub or "hub").lower())[:16].strip("-") or "hub"
    return f"hz-{slug}-" + hashlib.sha256(f"{hub}\x00{key}".encode()).hexdigest()[:40]


def agentcore_url(arn: str, region: str | None = None, qualifier: str = "DEFAULT") -> str:
    """A runtime's HTTPS invocation address, as AgentCore's protected resource
    metadata names it (the MCP runtime's token audience)."""
    from urllib.parse import quote

    region = region or arn.split(":")[3]
    return (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{quote(arn, safe='')}"
            f"/invocations?qualifier={quote(qualifier or 'DEFAULT', safe='')}")


def release() -> dict:
    """This process's Hubzoid version and image (``HUBZOID_IMAGE``, set by the
    deployment). A runner runs a call only for the same release."""
    import os

    from .. import __version__

    return {"hubzoid": __version__, "image": (os.environ.get("HUBZOID_IMAGE") or "").strip()}


def release_mismatch(asked: dict | None, own: dict) -> str:
    """Why a call for release ``asked`` may not run on ``own`` ('' when it may)."""
    asked = asked or {}
    if asked.get("hubzoid") != own.get("hubzoid"):
        return f"the hub service runs Hubzoid {asked.get('hubzoid')}, this runner {own.get('hubzoid')}"
    if asked.get("image") and own.get("image") and asked["image"] != own["image"]:
        return f"the hub service runs image {asked['image']}, this runner {own['image']}"
    return ""
