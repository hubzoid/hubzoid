"""Ratings from channels: a click in Slack, a reaction or a reply in WhatsApp.

A channel's file opts in with ``feedback: true``; the deployment's
``feedback.enabled`` still decides whether anything is collected. A rating
arrives like any delivery: verified on the exact bytes, committed once (so a
provider redelivery or a double click is one record), then routed here.

It is bound to the exact reply this connection delivered in that thread,
from our own records only: the recorded outside write (``hz_operations``)
whose provider message id is the one clicked or reacted to, in the same
thread. What the payload claims is checked against that, never trusted.

* **Slack** (agent connections): the click is by a Slack user who must be
  linked to an active account with access to the hub, the same rule as asking
  the agent. The rating is that account's rating of the native chat answer,
  the same record a web rating of it would update. The person gets a private
  confirmation; nothing is posted in the thread. "Not helpful" opens a dialog
  for reasons and a comment, following eve's Slack renderer.
* **WhatsApp** (intake): the sender is outside the organization and has no
  account. The rating is an external author's (one opaque id per sender and
  number), never the workflow owner's or the person who released the message.
  Nothing is sent back, so no approval or 24-hour window is involved.

Everything here is a rating. It never starts work, reaches a model or posts
to anyone but the person who clicked.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text

log = logging.getLogger("hubzoid.channels.feedback")

#: Slack ids: the feedback buttons' action, the "Not helpful" dialog
ACTION_ID = "hz_feedback"
DIALOG_ID = "hz_feedback_reason"
#: WhatsApp: the line a final reply ends with, and the emoji that rate
WHATSAPP_FOOTER = ("Rate this reply: react 👍 or 👎. To add a comment, reply to this message "
                   "starting with 👎.")
UP, DOWN = ("👍",), ("👎",)
_SKIN = re.compile("[\U0001F3FB-\U0001F3FF️]")

NOTICES = {
    "identity_unmapped": "I can't save your rating yet. Ask a Hubzoid administrator to link your Slack account.",
    "account_inactive": "I can't save your rating: your Hubzoid account is not active.",
    "hub_access_denied": "I can't save your rating: your Hubzoid account does not have access to this hub.",
    "feedback_off": "Ratings are turned off here, so nothing was saved.",
    "target_mismatch": "That rating doesn't match an answer in this thread, so nothing was saved.",
    "conflict": "Your rating changed at the same moment elsewhere. Nothing was overwritten. Try again.",
}


def emoji_rating(value: str) -> str | None:
    """positive, negative or None for a reaction or a reply's first character."""
    bare = _SKIN.sub("", (value or "").strip())
    if bare.startswith(UP):
        return "positive"
    if bare.startswith(DOWN):
        return "negative"
    return None


def _service(conn):
    from .. import db
    from ..chat import feedback as fb

    return fb.service_for(conn.hub_dir, db.operational_engine(Path(conn.hub_dir)), Path(conn.hub_dir).name.lower())


def delivered(conn, thread_id: str, provider_message_id: str) -> dict | None:
    """The reply this connection posted in this thread as the provider's
    message ``provider_message_id``: a succeeded ``channel.*`` operation."""
    from ..work_items import store as wstore

    if not provider_message_id or not thread_id:
        return None
    like = provider_message_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with wstore.engine(conn.hub_dir).connect() as c:
        ids = [r[0] for r in c.execute(text(
            "SELECT id FROM hz_operations WHERE hub=:h AND name LIKE 'channel.%' AND state='succeeded' "
            "AND target_json LIKE :t AND result_json LIKE :m ESCAPE '\\'"),
            {"h": conn.hub, "t": f'%"thread":"{thread_id}"%', "m": f"%{like}%"})]
    for op_id in ids:
        row = wstore.get_op(conn.hub_dir, op_id)
        target = (row or {}).get("target_json") or {}
        res = (row or {}).get("result_json") or {}
        res = res.get("result", res) if isinstance(res, dict) else {}
        if target.get("channel") == conn.id and target.get("thread") == thread_id and \
                provider_message_id in (res.get("message_id"), res.get("provider_message_id")):
            return row
    return None


def route(hub_dir, conn, row: dict, thread: dict, event, app) -> tuple[str, None] | None:
    """Route one rating event. None when it is not a rating of one of our
    replies after all (a WhatsApp reaction to something else): the caller
    handles it as an ordinary message."""
    if conn.provider == "slack":
        return _slack(hub_dir, conn, row, thread, event)
    if conn.provider == "whatsapp_cloud":
        return _whatsapp(hub_dir, conn, row, thread, event)
    from .router import _finish

    _finish(hub_dir, row["id"], "ignored", "feedback_unsupported")
    return "ignored", None


# -- Slack ------------------------------------------------------------------------------------

def _slack(hub_dir, conn, row, thread, event) -> tuple[str, None]:
    from ..chat import graph as g
    from ..chat.graph import GraphError
    from ..chat.store import conversations
    from . import router

    data = event.data
    svc = _service(conn)

    def stop(status: str, reason: str) -> tuple[str, None]:
        _ephemeral(conn, data, NOTICES.get(reason) or NOTICES["target_mismatch"])
        router._finish(hub_dir, row["id"], status, reason)
        return status, None

    if not conn.config.feedback or not svc.enabled():
        return stop("ignored", "feedback_off")
    person, reason = router.person_for(conn, event)
    if person is None:
        return stop("denied", reason)
    if router._hub_access(hub_dir, person.email) == "hub_access_denied":
        return stop("denied", "hub_access_denied")
    op = delivered(conn, thread["id"], data.get("message_ts") or "")
    ref = ((op or {}).get("target_json") or {}).get("ref") or ""
    if op is None or op.get("name") != "channel.feedback" or ref != f"run:{data.get('run_id')}":
        return stop("denied", "target_mismatch")
    with svc.engine.connect() as c:
        run = c.execute(sa.select(g.runs.c.message_id, g.runs.c.conversation_id, g.runs.c.branch_id)
                        .where(g.runs.c.id == data["run_id"])).first()
        conv = c.execute(sa.select(conversations.c.agent, conversations.c.deleted_at)
                         .where(conversations.c.id == run.conversation_id)).first() if run else None
    if run is None or conv is None or conv.deleted_at is not None:
        return stop("denied", "target_mismatch")
    action = data.get("action")
    rating = "positive" if action == "up" else "negative"
    kwargs: dict = {}
    if action == "up":
        kwargs = {"reason_ids": [], "comment": ""}
    elif action == "reason":
        kwargs = {"reason_ids": list(data.get("reasons") or []), "comment": data.get("comment") or ""}
    try:
        out = svc.record(target_kind="message", target_id=run.message_id, author_id=person.id, source="slack",
                         rating=rating, conversation_id=run.conversation_id, branch_id=run.branch_id,
                         agent_id=conv.agent, channel_id=conn.id, thread_id=thread["id"],
                         request_key=f"slack:{event.semantic_key}"[:160],
                         metadata={"via": "slack", "message_ts": data.get("message_ts")}, **kwargs)
    except GraphError as exc:
        if exc.code == "feature_disabled":
            return stop("ignored", "feedback_off")
        _ephemeral(conn, data, f"Not saved: {exc.message}")
        router._finish(hub_dir, row["id"], "failed", exc.code)
        return "failed", None
    if out["outcome"] == "conflict":
        return stop("failed", "conflict")
    if action == "down" and _open_dialog(conn, data):
        pass    # the dialog says what was saved and asks what was wrong
    elif action == "reason":
        _ephemeral(conn, data, "Thanks. Your reason is saved with your rating.")
    else:
        _ephemeral(conn, data, "Thanks. Saved as helpful." if rating == "positive" else
                   "Thanks. Saved as not helpful.")
    router._finish(hub_dir, row["id"], "routed", None, target="feedback", outcome=out["outcome"],
                   feedback_id=(out.get("feedback") or {}).get("id"))
    return "routed", None


def slack_blocks(run_id: str) -> list[dict]:
    """The feedback buttons posted under a completed answer (eve's renderer
    uses the same Slack element). The block id names the run; a click is
    still checked against the recorded post."""
    return [{"type": "context_actions", "block_id": f"hzfb:{run_id}"[:255], "elements": [{
        "type": "feedback_buttons", "action_id": ACTION_ID,
        "positive_button": {"text": {"type": "plain_text", "text": "Helpful"}, "value": "up",
                            "accessibility_label": "Mark this answer as helpful"},
        "negative_button": {"text": {"type": "plain_text", "text": "Not helpful"}, "value": "down",
                            "accessibility_label": "Mark this answer as not helpful"}}]}]


def slack_fallback_blocks(run_id: str) -> list[dict]:
    """Plain buttons, for a workspace that refuses the feedback element."""
    return [{"type": "actions", "block_id": f"hzfb:{run_id}"[:255], "elements": [
        {"type": "button", "action_id": f"{ACTION_ID}_up", "value": "up",
         "text": {"type": "plain_text", "text": "Helpful"}},
        {"type": "button", "action_id": f"{ACTION_ID}_down", "value": "down",
         "text": {"type": "plain_text", "text": "Not helpful"}}]}]


def _dialog(data: dict, reasons: list[str]) -> dict:
    labels = {"incorrect": "Incorrect", "missing_source": "Missing source", "other": "Something else",
              "accurate": "Accurate", "useful": "Useful"}
    options = [{"text": {"type": "plain_text", "text": labels.get(r, r.replace("_", " ").capitalize())},
                "value": r} for r in reasons if r not in ("accurate", "useful")][:10]
    meta = json.dumps({"run_id": data.get("run_id"), "channel": data.get("channel"),
                       "thread_ts": data.get("thread_ts"), "message_ts": data.get("message_ts")})
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn",
                                                       "text": "Saved as *not helpful*. What was wrong? (optional)"}}]
    if options:
        blocks.append({"type": "input", "block_id": "reasons", "optional": True,
                       "label": {"type": "plain_text", "text": "Reasons"},
                       "element": {"type": "checkboxes", "action_id": "value", "options": options}})
    blocks.append({"type": "input", "block_id": "comment", "optional": True,
                   "label": {"type": "plain_text", "text": "Comment"},
                   "element": {"type": "plain_text_input", "action_id": "value", "multiline": True,
                               "max_length": 3000}})
    return {"type": "modal", "callback_id": DIALOG_ID, "private_metadata": meta,
            "title": {"type": "plain_text", "text": "Feedback"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Skip"}, "blocks": blocks}


def _open_dialog(conn, data: dict) -> bool:
    from ..chat.feedback import _settings
    from .slack.adapter import client

    if not data.get("trigger_id"):
        return False
    try:
        reasons = _settings(conn.hub_dir)["feedback"]["reasons"]
        client(conn).views_open(trigger_id=data["trigger_id"], view=_dialog(data, reasons))
        return True
    except Exception:  # noqa: BLE001 - the trigger expires after 3 s: the rating is saved anyway
        log.info("channel %s: the feedback dialog did not open", conn.id, exc_info=True)
        return False


def _ephemeral(conn, data: dict, message: str) -> None:
    """A private line to the person who clicked. Never a thread post."""
    from .slack.adapter import client

    if not data.get("channel") or not data.get("user"):
        return
    try:
        kwargs = {"channel": data["channel"], "user": data["user"], "text": message}
        if data.get("thread_ts"):
            kwargs["thread_ts"] = data["thread_ts"]
        client(conn, writes=True).chat_postEphemeral(**kwargs)
    except Exception:  # noqa: BLE001 - a confirmation is a courtesy
        log.info("channel %s: could not confirm a rating privately", conn.id, exc_info=True)


def post_slack_prompt(conn, thread: dict, run_id: str) -> None:
    """After a completed answer: one small message with the feedback buttons,
    recorded like any post, so a click can be checked against it."""
    from . import replies

    if not conn.config.feedback or conn.adapter.feedback_target != "agent" or not _service(conn).enabled():
        return
    try:
        replies.post(conn, thread, kind="feedback", text="Was this answer helpful?", key=f"feedback:{run_id}",
                     ref=f"run:{run_id}", blocks=slack_blocks(run_id))
    except Exception:  # noqa: BLE001 - the answer is delivered; the prompt is optional
        log.warning("channel %s: could not post the feedback buttons for %s", conn.id, run_id, exc_info=True)


# -- WhatsApp ---------------------------------------------------------------------------------

def mask(external_id: str) -> str:
    digits = re.sub(r"\D", "", external_id or "")
    return f"WhatsApp sender ··{digits[-4:]}" if len(digits) >= 4 else "WhatsApp sender"


def _whatsapp(hub_dir, conn, row, thread, event) -> tuple[str, None] | None:
    from ..chat.graph import GraphError
    from .router import _finish

    data = event.data
    op = delivered(conn, thread["id"], data.get("target") or "")
    if op is None or op.get("name") != "channel.result":
        return None     # not a rating of a reply we sent: an ordinary message
    if not conn.config.feedback:
        return None
    svc = _service(conn)
    action = data.get("action")
    rating = None if action == "remove" else ("positive" if action == "up" else "negative")

    def off() -> tuple[str, None]:
        # A rating of our reply while collection is off: nothing is collected,
        # and it never becomes Intake work. Removing one still works (below).
        _finish(hub_dir, row["id"], "ignored", "feedback_off", target="feedback")
        return "ignored", None

    if rating is not None and not svc.enabled():
        return off()
    kwargs: dict = {}
    if data.get("comment") is not None:
        kwargs = {"comment": data["comment"], "reason_ids": None}
    try:
        out = svc.record(target_kind="channel_reply", target_id=op["id"], author_kind="external",
                         author_id=f"ext:{thread['id']}", author_label=mask(event.actor.external_id
                                                                             if event.actor else ""),
                         source="whatsapp", rating=rating, agent_id=conn.config.workflow, channel_id=conn.id,
                         thread_id=thread["id"], request_key=f"wa:{event.message_id}"[:160],
                         metadata={"via": "whatsapp", "provider_message_id": event.message_id},
                         order=float(data.get("timestamp") or 0), **kwargs)
    except GraphError as exc:
        if exc.code == "feature_disabled":    # turned off while this was being routed
            return off()
        _finish(hub_dir, row["id"], "failed", exc.code)
        return "failed", None
    _finish(hub_dir, row["id"], "routed", None, target="feedback", outcome=out["outcome"],
            feedback_id=(out.get("feedback") or {}).get("id"), replied="none")
    return "routed", None


def whatsapp_footer(conn, kind: str, existing_text: str | None) -> str:
    """The line a final WhatsApp reply ends with while ratings are on. A reply
    already recorded (waiting for approval) keeps exactly the text it was
    recorded with, so turning ratings on or off never changes a reviewed reply."""
    if existing_text is not None:
        return WHATSAPP_FOOTER if existing_text.rstrip().endswith(WHATSAPP_FOOTER) else ""
    if kind != "result" or not conn.config.feedback or conn.adapter.feedback_target != "workflow":
        return ""
    return WHATSAPP_FOOTER if _service(conn).enabled() else ""
