"""``ask_question``: the agent asks the person one question (eve's tool).

Same shape and guidance as eve's ``ask_question``
(``packages/eve/src/tools/provided/ask-question.ts``): one focused question,
two or three options with the recommended one first, and the person can
always type their own answer. With nobody to answer (a scheduled run) it
says so and the agent goes on with its best judgment.

Questions are on by default and can be turned off per hub or per agent in
``hitl.yaml`` (``questions: false``); then the tool is not offered.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents import FunctionTool

from . import config as cfglib
from . import gate, service

DESCRIPTION = "\n".join([
    "Ask the user a question and wait for the answer. Use this only when you cannot make good progress "
    "without the user's input: to choose between materially different approaches, fill in a missing "
    "requirement, or confirm something risky or irreversible. Do not ask for information you can find with "
    "your other tools, and do not ask the user to confirm routine steps.",
    "",
    "Usage:",
    "- Ask one focused question that includes the context needed to answer it.",
    "- For a decision, offer 2-3 mutually exclusive options. Put the option you recommend first and end its "
    "label with \"(Recommended)\".",
    "- Do not add an \"Other\" option; the user can always type their own answer.",
    "- After asking, end your turn. The answer arrives as the user's next message.",
    "- If the status is \"unavailable\", no one can answer now; continue with your best judgment and state "
    "the assumption you made.",
])

SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 1000,
                     "description": "The question, with the context needed to answer it."},
        "options": {
            "type": "array", "minItems": 2, "maxItems": 3,
            "items": {"type": "object", "properties": {
                "label": {"type": "string", "minLength": 1, "maxLength": 120},
                "description": {"type": "string", "maxLength": 200,
                                "description": "One short sentence on the impact of choosing this option."}},
                "required": ["label"], "additionalProperties": False},
        },
    },
    "required": ["question"],
    "additionalProperties": False,
}


def _validate(raw) -> tuple[str, list | None]:
    data = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
    question = str(data.get("question") or "").strip()
    if not question:
        raise ValueError("question is required")
    options = data.get("options") or None
    if options is not None:
        if not isinstance(options, list) or not 2 <= len(options) <= 3:
            raise ValueError("offer 2 or 3 options, or none")
        options = [{"label": str(o.get("label", "")).strip()[:120],
                    "description": str(o.get("description") or "").strip()[:200]} for o in options]
        if any(not o["label"] for o in options):
            raise ValueError("every option needs a label")
    return question[:1000], options


def ask_question_tool(hub_dir: Path, *, agent: str | None = None) -> FunctionTool | None:
    """The tool, or None when this hub (or agent) has questions turned off."""
    hub_dir = Path(hub_dir)
    if not cfglib.load(hub_dir).questions_for(agent):
        return None

    async def invoke(_ctx, raw):
        try:
            question, options = _validate(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        who = gate.caller()
        if who is None or not who.present or gate.suspended():
            return json.dumps({"status": "unavailable", "message":
                               "No one can answer now. Continue with your best judgment and state the "
                               "assumption you made."})
        req = await asyncio.to_thread(
            service.create, hub_dir, gate.hub_id_of(hub_dir), who, kind="question", title=question[:200],
            question=question, options=options, approvers=("requester",))
        gate.note_waiting("ask_question", req)
        return json.dumps({"status": "waiting", "request_id": req["id"], "kind": "question",
                           "message": "Your question is shown to the user. End your turn now; their answer "
                                      "arrives as their next message."})

    return FunctionTool(name="ask_question", description=DESCRIPTION, params_json_schema=SCHEMA,
                        on_invoke_tool=invoke, strict_json_schema=False)
