"""A workflow run started from a chat ("Run for me") reports back to that chat.

When the person starts a run from chat, the run's envelope keeps where it
came from (``env["chat"]``: conversation, branch, account). Two moments then
post into that chat, as one assistant message each:

* the run asks the person something (``hub.ask``, ``hub.approve``,
  ``hub.review_plan``) after the chat stopped waiting: the card appears in the
  chat, as well as under "Waiting for you";
* the run ends after the chat stopped waiting, or after it asked something:
  its result (or why it stopped) appears in the chat.

Message ids are fixed by the request or run id, so a replayed run never posts
twice. If a reply is being written in that chat at that moment, the post is
retried briefly; failing that, the request stays in "Waiting for you" and the
result in the person's reports. Best effort: never changes the run's outcome.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("hubzoid.workflows")


def origin(slot) -> dict | None:
    """The chat a tool call runs in (from the reply's session slot), or None."""
    info = getattr(slot, "hitl", None) if slot is not None else None
    if not info or not info.get("conversation_id") or not info.get("branch_id"):
        return None
    return {"conversation_id": info["conversation_id"], "branch_id": info["branch_id"],
            "account_id": info.get("account_id") or ""}


def _mid(kind: str, key: str) -> str:
    return "m_" + hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()[:24]


def _post(hub_dir: Path, chat: dict, message_id: str, parts: list, *, tries: int = 1,
          unless_mentioned: str | None = None) -> str:
    from ..chat import store as store_mod
    from ..chat.engine import ChatEngine

    engine = ChatEngine(hub_dir, store_mod.for_hub(hub_dir), hub_dir.name.lower())
    out = "busy"
    for i in range(tries):
        out = engine.post_from_run(chat["conversation_id"], branch_id=chat["branch_id"],
                                   actor_id=chat["account_id"], message_id=message_id, parts=parts,
                                   unless_mentioned=unless_mentioned)
        if out != "busy":
            return out
        if i + 1 < tries:
            time.sleep(3)
    return out


def post_request(hub_dir, env: dict, workflow: str, req: dict) -> None:
    """Show a run's new request in the chat that started the run, unless that
    chat already shows it (the reply that started the run waited for it)."""
    chat = (env or {}).get("chat")
    if not chat or req.get("status") != "pending":
        return
    from .runtime import title_for

    part = {"type": "tool-call", "toolCallId": f"wf-{req['id'][:12]}", "toolName": workflow,
            "args": {}, "argsText": "{}",
            "result": {"status": "waiting", "request_id": req["id"], "kind": req.get("kind") or "approval"}}
    text = {"type": "text", "text": f"{title_for(workflow)} is waiting for you:"}
    def run():
        try:
            # A reply may be being written (the one that started the run shows
            # the card itself): wait for it, up to two minutes.
            _post(Path(hub_dir), chat, _mid("request", req["id"]), [text, part], tries=40,
                  unless_mentioned=req["id"])
        except Exception:  # noqa: BLE001 - it is still under "Waiting for you"
            log.warning("workflows: could not show request %s in its chat", req.get("id"), exc_info=True)

    import threading

    threading.Thread(target=run, name=f"hz-chat-link-{req['id'][:8]}", daemon=True).start()


def _readable(result) -> str:
    """A run's return value for a person: text as is, a mapping as lines."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        lines = []
        for key, value in result.items():
            if value is None or key.startswith("_"):
                continue
            shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            lines.append(f"- {str(key).replace('_', ' ').capitalize()}: {shown}")
        return "\n".join(lines)
    return json.dumps(result, ensure_ascii=False, default=str)


def post_result(hub_dir, env: dict, workflow: str, run_id: str, *, ok: bool, result=None,
                cause: str | None = None, published: list | None = None, tool_wait: float = 60.0) -> None:
    """Bring a run's outcome back to the chat that started it, when the chat
    did not already show it (the run outlived the chat's wait, or it asked
    something on the way)."""
    chat = (env or {}).get("chat")
    if not chat:
        return
    started = (env or {}).get("started_at") or 0
    asked = False
    try:
        from ..hitl import service

        asked = service.count_for_workflow(Path(hub_dir), Path(hub_dir).name.lower(), run_id, None) > 0
    except Exception:  # noqa: BLE001
        pass
    if not asked and started and time.time() - started <= tool_wait:
        return  # the chat waited and showed the outcome itself
    from .outcomes import CAUSES, links_text
    from .runtime import title_for

    title = title_for(workflow)
    if ok:
        body = "\n\n".join(p for p in (_readable(result), links_text(list(published or []))) if p)
        text = f"{title} finished." + (f"\n\n{body}" if body else "")
    else:
        text = f"{title} stopped: {CAUSES.get(cause or 'error', CAUSES['error'])}."
    try:
        _post(Path(hub_dir), chat, _mid("result", run_id), [{"type": "text", "text": text}], tries=5)
    except Exception:  # noqa: BLE001 - it is still in the person's runs and reports
        log.warning("workflows: could not post the result of %s to its chat", run_id, exc_info=True)
