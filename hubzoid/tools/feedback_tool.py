"""record_feedback: save the person's own rating of an earlier answer, when they ask.

Offered only when the deployment turns it on (``feedback.tool`` in
``chat/config.yaml``, off by default) and feedback is collected at all
(``feedback.enabled``). Both are checked again on every call, so turning
either off stops the tool at once, whatever runtime built it.

What is trusted comes from the run, never from the model:

* the author is the person the chat reply acts for (``hitl.gate.caller``,
  set by the chat engine before the model starts, and the request identity);
* the conversation, branch and the reply being written are that run's;
* the person's words are their latest message in that conversation.

The model supplies only the rating, the person's exact words asking for it
(``quote``), optionally their exact words as the comment, reasons, and how
many answers back (``answers_back``, default the previous one). Both quoted
texts must appear in the person's latest message: the tool cannot invent a
vote or put words in their mouth. The target is found by walking back along
the current branch, never another branch or conversation, and never the
reply being written or a reply that only confirmed a saved rating.

Like Mastra's feedback records, a rating is anchored to one exact result and
keeps its source and author. It is a person's assessment: it never becomes
memory, a Hub edit or an action.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger("hubzoid.tools.feedback")

NAME = "record_feedback"
MAX_BACK = 5
MAX_STEPS = 400

DESCRIPTION = (
    "Save the person's rating of one of your earlier answers, ONLY when they explicitly ask you to save, record or "
    "send feedback or a rating ('save negative feedback on your last answer, the price was outdated'). Never call it "
    "on your own initiative, from general praise or complaints, or because a correction was made: a correction is "
    "not a rating unless they ask to record it. quote is their exact words asking for it; comment, if any, is their "
    "exact words about what was right or wrong. Both must be copied from their latest message, not paraphrased. "
    "answers_back is 1 for your previous answer (the default), 2 for the one before it. If they don't say which "
    "answer and it isn't the previous one, ask them. Tell them the result in one sentence."
)

PARAMS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rating": {"type": "string", "enum": ["positive", "negative"],
                   "description": "positive (helpful) or negative (not helpful), as the person said."},
        "quote": {"type": "string", "description": "The person's exact words asking you to save this feedback, "
                                                   "copied from their latest message."},
        "comment": {"type": "string", "description": "Optional. The person's exact words about the answer, "
                                                     "copied from their latest message."},
        "reason_ids": {"type": "array", "items": {"type": "string"},
                       "description": "Optional reason ids: incorrect, missing_source, other, accurate, useful."},
        "answers_back": {"type": "integer", "minimum": 1, "maximum": MAX_BACK,
                         "description": "Which earlier answer: 1 = your previous answer (default)."},
    },
    "required": ["rating", "quote"],
}


def _norm(text: str) -> str:
    """Lowercase, one space between words, quotes and outer punctuation gone,
    so a faithful copy matches whatever the model did with case or quotes."""
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = " ".join(text.lower().split())
    return text.strip(" \"'`.,;:!?")


def _said(words: str, message: str) -> bool:
    w = _norm(words)
    return len(w) >= 3 and w in _norm(message)


def _settings(hub_dir: Path) -> dict:
    from ..chat.feedback import _settings as chat_settings

    return chat_settings(hub_dir)["feedback"]


def offered(hub_dir: Path) -> bool:
    """Whether to add the tool to an agent built now."""
    try:
        cfg = _settings(hub_dir)
    except Exception:  # noqa: BLE001 - no settings store (CLI, tests): not offered
        return False
    return bool(cfg.get("enabled") and cfg.get("tool"))


def result(status: str, message: str, **extra) -> str:
    return json.dumps({"status": status, "message": message, **extra}, ensure_ascii=False)


def _caller(hub_dir: Path):
    """The person in the chat this call belongs to, or (None, why not)."""
    from ..access.identity import current_identity
    from ..hitl.gate import caller

    c = caller()
    ident = current_identity()
    if c is None or not c.present or c.origin != "chat" or not c.conversation_id or not c.message_id:
        return None, "Feedback can only be saved by a person in a chat conversation."
    if ident.account_id and ident.account_id != c.account_id:
        return None, "This run does not act for the person in the chat."
    return c, ""


def record(hub_dir: Path, args: dict) -> str:
    """The tool's work, off the event loop. Returns the JSON result text."""
    import sqlalchemy as sa

    from .. import db
    from ..chat import feedback as fb
    from ..chat.store import conversations, messages

    hub_dir = Path(hub_dir)
    try:
        cfg = _settings(hub_dir)
    except Exception:  # noqa: BLE001
        return result("denied", "Feedback settings are unavailable right now. Nothing was saved.")
    if not cfg.get("enabled"):
        return result("denied", "Feedback is turned off for this deployment. Nothing was saved.")
    if not cfg.get("tool"):
        return result("denied", "Saving feedback from chat is turned off. The person can use the thumbs buttons.")
    who, why = _caller(hub_dir)
    if who is None:
        return result("denied", why)
    rating = args.get("rating")
    if rating not in ("positive", "negative"):
        return result("denied", "rating is positive or negative.")
    quote, comment = str(args.get("quote") or ""), args.get("comment")
    if comment is not None and not isinstance(comment, str):
        return result("denied", "comment is text.")
    comment = (comment or "").strip() or None
    try:
        back = int(args.get("answers_back") or 1)
    except (TypeError, ValueError):
        back = 0
    if not 1 <= back <= MAX_BACK:
        return result("denied", f"answers_back is 1 to {MAX_BACK}.")
    engine = db.operational_engine(hub_dir)
    with engine.connect() as conn:
        conv = conn.execute(sa.select(conversations).where(conversations.c.id == who.conversation_id)).first()
        reply = conn.execute(sa.select(messages).where(messages.c.id == who.message_id)).first()
        inbound = conn.execute(sa.select(messages).where(messages.c.id == reply.parent_id)).first() \
            if reply is not None and reply.parent_id else None
        if conv is None or conv.deleted_at is not None or reply is None or reply.conversation_id != conv.id \
                or inbound is None or inbound.role != "user":
            return result("denied", "This reply has no message from the person to act on.")
        if conv.temporary:
            return result("denied", "Temporary conversations don't keep feedback.")
        svc = fb.service_for(hub_dir, engine, hub_dir.name.lower())   # the chat context's hub name
        # This reply handles a feedback request, whatever the outcome: it is
        # never "the previous answer" for a later request.
        svc.note_tool_reply(conv.id, reply.id)
        said = inbound.text or ""
        if not _said(quote, said):
            return result("denied", "Only save feedback the person asked for. quote must be their exact words "
                                    "from their latest message. Nothing was saved.")
        if comment and not _said(comment, said):
            return result("denied", "comment must be the person's exact words from their latest message. "
                                    "Nothing was saved.")
        skip = svc.tool_replies(conv.id)
        answers, cur, steps = [], inbound.parent_id, 0
        while cur and len(answers) < back and steps < MAX_STEPS:
            steps += 1
            m = conn.execute(sa.select(messages).where(messages.c.id == cur)).first()
            if m is None or m.conversation_id != conv.id:
                break
            if m.role == "assistant" and m.deleted_at is None and m.status != "running" and m.id not in skip:
                answers.append(m)
            cur = m.parent_id
    if len(answers) < back:
        return result("needs_target", "There is no such earlier answer in this conversation. Ask the person which "
                                      "answer they mean. Nothing was saved.")
    target = answers[back - 1]
    agent = getattr(conv, "agent", None)
    out = svc.record(target_kind="message", target_id=target.id, author_id=who.account_id, source="tool",
                     rating=rating, conversation_id=conv.id, branch_id=who.branch_id, agent_id=agent,
                     reason_ids=list(args.get("reason_ids") or []), comment=comment,
                     request_key=f"tool:{inbound.id}",
                     metadata={"via": "tool", "inbound_message_id": inbound.id, "quote": quote.strip()[:500],
                               "reply_message_id": who.message_id})
    excerpt = fb._excerpt(target.text or "")
    if out["outcome"] == "conflict":
        return result("conflict", "The rating changed at the same moment elsewhere. Nothing was overwritten. "
                                  "Ask the person to check it with the thumbs buttons.")
    words = {"saved": "Saved", "updated": "Updated", "unchanged": "Already saved"}[out["outcome"]]
    previous = out.get("previous")
    note = f" (it was {previous})" if out["outcome"] == "updated" and previous and previous != rating else ""
    return result(out["outcome"], f"{words}: {rating} feedback on the answer that starts "
                                  f"\"{(excerpt or '')[:80]}\"{note}. It is a rating for reviewers, not a change "
                                  "to how the agent works.", rating=rating, answer_id=target.id)


def make(ctx) -> list:
    """The tool, when the deployment offers it. A rebuild (or restart) adds or
    removes it; every call also checks the live setting."""
    from agents import FunctionTool

    hub_dir = Path(ctx.hub_dir)
    if not offered(hub_dir):
        return []

    async def invoke(_ctx, input_str: str) -> str:
        try:
            args = json.loads(input_str or "{}")
        except ValueError:
            return result("denied", "The arguments are not valid JSON.")
        if not isinstance(args, dict):
            return result("denied", "The arguments are an object.")
        try:
            return await asyncio.to_thread(record, hub_dir, args)
        except Exception as exc:  # noqa: BLE001 - the chat goes on; a Err carries its own words
            message = getattr(exc, "message", None)
            if message:
                return result("denied", f"Not saved: {message}")
            log.exception("record_feedback failed")
            return result("denied", "Not saved: something went wrong. The person can use the thumbs buttons.")

    return [FunctionTool(name=NAME, description=DESCRIPTION, params_json_schema=PARAMS, on_invoke_tool=invoke,
                         strict_json_schema=False)]

