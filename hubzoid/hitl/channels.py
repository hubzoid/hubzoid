"""Approvals and questions in messaging channels (WhatsApp, Slack).

Following eve's channel handling: a request is shown in the person's own
private chat with the exact action, and answered there.

* WhatsApp (a 1:1 chat with the verified number): the action is spelled out
  and the person replies ``1`` to approve or ``2`` to reject, or the number of
  an option, or types an answer to a question. Only the verified sender who
  asked can answer, only while exactly one request waits in that chat, and
  each WhatsApp message is handled once (the harness drops repeats), so a late
  "1" can never answer a newer request. Writing anything else instead of
  answering an approval cancels it, as in the chat.
* Slack: the exact action and Approve or Reject buttons go to the requester in
  a direct message (a shared thread only gets a line saying it waits). A
  button carries the request id, revision and fingerprint, and is honoured only
  for the Slack user whose verified email is the requester's.
"""
from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa

from . import service, store

APPROVE, REJECT = "1", "2"


def _hub(hub_dir: Path) -> str:
    return Path(hub_dir).name.lower()


def pending_in_chat(hub_dir: Path, chat_id: str, email: str, *, since: float | None = None) -> list[dict]:
    r = store.requests.c
    q = sa.select(store.requests).where(r.hub_id == _hub(hub_dir), r.conversation_id == chat_id,
                                        r.status == "pending")
    if since is not None:
        q = q.where(r.created_at >= since)
    with service.engine(hub_dir).connect() as conn:
        rows = [store.row_dict(x) for x in conn.execute(q.order_by(r.created_at)).fetchall()]
    account_id = _account_for(hub_dir, email)
    return [x for x in rows if service.eligible(x, account_id, email)]


def describe(req: dict) -> str:
    """The request as plain text for a messaging app: the exact action."""
    if req["kind"] == "question":
        lines = [req.get("question") or req["title"]]
        for i, opt in enumerate(req.get("options") or [], start=1):
            extra = f" ({opt['description']})" if opt.get("description") else ""
            lines.append(f"{i}. {opt['label']}{extra}")
        lines.append("Reply with a number, or type your own answer." if req.get("options")
                     else "Reply with your answer.")
        return "\n".join(lines)
    args = json.dumps(req.get("arguments") or {}, ensure_ascii=False, indent=1, default=str)
    return (f"Approval needed: {req['title']}\n{args}\n"
            f"Reply {APPROVE} to approve or {REJECT} to reject.")


def _account_for(hub_dir: Path, email: str | None) -> str:
    """The account that holds this channel sender's email now. A request an
    account made is decided by that account only, so a new account that reuses
    a deleted one's email can't decide the old one's requests."""
    from ..auth import users

    try:
        return ((users.find_by_email(Path(hub_dir), email) or {}).get("id") or "") if email else ""
    except Exception:  # noqa: BLE001 - no account store: an email-only sender
        return ""


def answer_by_reply(hub_dir: Path, *, chat_id: str, email: str, text: str, via: str) -> dict | None:
    """Treat a channel message as the answer to the one waiting request, if it
    is one. Returns the settled request, or None for an ordinary message (in
    which case waiting approvals are cancelled so the message steers)."""
    waiting = pending_in_chat(hub_dir, chat_id, email)
    if len(waiting) != 1:
        if waiting and text.strip() not in (APPROVE, REJECT):
            for req in waiting:
                service.cancel(hub_dir, _hub(hub_dir), req["id"], system=True,
                               reason="The person wrote a new message instead.")
        return None
    req = waiting[0]
    reply = (text or "").strip()
    try:
        if req["kind"] == "question":
            options = req.get("options") or []
            if reply.isdigit() and 1 <= int(reply) <= len(options):
                reply = options[int(reply) - 1]["label"]
            if not reply:
                return None
            return service.decide(hub_dir, _hub(hub_dir), req["id"], account_id=_account_for(hub_dir, email), email=email, decision="answer",
                                  expected_revision=req["revision"], digest=req["digest"], answer=reply, via=via)
        if reply in (APPROVE, REJECT):
            return service.decide(hub_dir, _hub(hub_dir), req["id"], account_id=_account_for(hub_dir, email), email=email,
                                  decision="approve" if reply == APPROVE else "reject",
                                  expected_revision=req["revision"], digest=req["digest"], via=via)
    except service.RequestError:
        return None
    service.cancel(hub_dir, _hub(hub_dir), req["id"], system=True, reason="The person wrote a new message instead.")
    return None


def mark_continued(hub_dir: Path, request: dict) -> None:
    service.mark_continued(Path(hub_dir), request["id"])


# -- Slack --------------------------------------------------------------------------
ACTION_PREFIX = "hz_hitl_"


def _page() -> str:
    from ..appmode import public_url

    return (public_url() or "").rstrip("/") + "/requests"


def slack_blocks(req: dict) -> list[dict]:
    """Block Kit for one request: the exact action, then its controls. The
    button value names the request, revision and fingerprint it was shown with."""
    ref = f"{req['id']}|{req['revision']}|{req['digest']}"
    if req["kind"] == "question":
        buttons = [{"type": "button", "action_id": f"{ACTION_PREFIX}answer_{i}",
                    "text": {"type": "plain_text", "text": opt["label"][:75]},
                    "value": f"{ref}|{i}"} for i, opt in enumerate(req.get("options") or [])]
        body = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{req.get('question') or req['title']}*"}}]
        if buttons:
            body.append({"type": "actions", "elements": buttons})
        body.append({"type": "context", "elements": [{"type": "mrkdwn",
                                                      "text": f"To give another answer, open <{_page()}|Hubzoid>."}]})
        return body
    args = json.dumps(req.get("arguments") or {}, ensure_ascii=False, indent=1, default=str)
    if len(args) > 2800:
        # Never approve what the card can't show: decide in Hubzoid, where it is shown in full.
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Approval needed:* {req['title']}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{args[:1500]}…```\nThis is too long "
                                         f"to show here. Review and decide it in <{_page()}|Hubzoid>."}},
        ]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Approval needed:* {req['title']}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{args}```"}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "action_id": f"{ACTION_PREFIX}approve",
             "text": {"type": "plain_text", "text": "Approve"}, "value": ref},
            {"type": "button", "action_id": f"{ACTION_PREFIX}reject",
             "text": {"type": "plain_text", "text": "Reject"}, "value": ref}]},
    ]


def slack_decide(hub_dir: Path, *, action_id: str, value: str, email: str | None) -> dict:
    """Settle a request from a Slack button. Raises RequestError when the
    clicker is not the requester, or the card is stale."""
    if not email:
        raise service.RequestError(403, "human_action_required",
                                   "Your Slack account is not linked to a Hubzoid account. Decide in Hubzoid.")
    parts = value.split("|")
    if len(parts) < 3:
        raise service.RequestError(400, "invalid_request", "This button is not a Hubzoid request.")
    rid, revision, digest = parts[0], int(parts[1]), parts[2]
    req = service.get(Path(hub_dir), _hub(hub_dir), rid)
    if req is None:
        raise service.RequestError(404, "not_found", "No such request.")
    kind = action_id[len(ACTION_PREFIX):]
    if kind.startswith("answer_"):
        options = req.get("options") or []
        idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1
        if not 0 <= idx < len(options):
            raise service.RequestError(422, "validation_failed", "That option is no longer offered.")
        return service.decide(Path(hub_dir), _hub(hub_dir), rid, account_id=_account_for(hub_dir, email), email=email, decision="answer",
                              expected_revision=revision, digest=digest, answer=options[idx]["label"], via="slack")
    decision = {"approve": "approve", "reject": "reject"}.get(kind)
    if decision is None:
        raise service.RequestError(400, "invalid_request", "Unknown action.")
    return service.decide(Path(hub_dir), _hub(hub_dir), rid, account_id=_account_for(hub_dir, email), email=email, decision=decision,
                          expected_revision=revision, digest=digest, via="slack")
