"""Core-shipped `call_jev` chat tool: typed decisions from Jev, gated by `jev`.

Every hub has the tool, but it is disabled until an administrator grants the
`jev` capability (Console label: Call Jev), exactly like
`remember` and `curator`. Callers without the grant never see it, and a call
that reaches it anyway is refused and logged by the same access guard as
`restricted/` tools. Granting agent entry (`use_hub`) or access management does
not include it.

It is the same adapter as a workflow's `hub.call_jev` (`hubzoid/jev.py`), with
the same dedicated JEV_OPENROUTER_API_KEY and the same checks, so a question
gets the same answer shape in chat and in workflows. Each call writes a usage
row (kind `jev`) attributed to the person, their surface and their chat.
Failures come back as readable text, never as an empty answer.
"""
from __future__ import annotations

import asyncio
import json
import logging

from agents.tool import FunctionTool

from .. import _request_ctx
from .. import jev as jevlib

log = logging.getLogger("hubzoid")

# The capability a caller needs, shown in Console as Call Jev.
JEV_PERMISSION = "jev"
TOOL_NAME = "call_jev"

DESCRIPTION = (
    "Ask Jev, a decision model, typed questions about a piece of text and get "
    "answers with probabilities instead of prose. Use it when the user asks to "
    "check whether something holds, pick one of several labels, or rate "
    "something on an ordered scale. One call can ask several questions of "
    "different types; each answer comes back under its question's name. "
    "noul: does it hold? The answer's `noul` is the probability of yes. "
    "choice: which label fits? `criteria` maps two or more labels to when to pick "
    "each; the answer's `choice` is one of them. score: where on a scale? "
    "`criteria` lists two or more levels, lowest first; the answer's `score` "
    "runs from 0 to the last level's index. Probabilities are signals, not proof."
)

PARAMS_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {
            "type": "string",
            "description": "The text to judge, for example a ticket or a message. JSON as text is fine.",
        },
        "questions": {
            "type": "object",
            "description": "Question name -> question. Answers come back under the same names.",
            "minProperties": 1,
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(jevlib.TYPES)},
                    "instructions": {"type": "string", "description": "The question itself."},
                    "criteria": {
                        "description": ('noul: optional {"true": "...", "false": "..."}. '
                                        'choice: {"label": "when to pick it", ...}, two or more. '
                                        'score: ["lowest level", ..., "highest level"], two or more.'),
                        "anyOf": [
                            {"type": "object", "additionalProperties": {"type": "string"}},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                },
                "required": ["type", "instructions"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["state", "questions"],
    "additionalProperties": False,
}


def make(ctx) -> list[FunctionTool]:
    hub_dir = ctx.hub_dir

    async def _invoke(_tool_ctx, args_json: str) -> str:
        from ..access.identity import current_identity

        try:
            args = json.loads(args_json or "{}")
        except ValueError:
            return f"[{TOOL_NAME} failed: the arguments are not valid JSON]"
        if not isinstance(args, dict):
            return f"[{TOOL_NAME} failed: the arguments must be an object with state and questions]"
        ident = current_identity()
        spec = {"state": args.get("state"), "questions": args.get("questions")}
        try:
            data = await asyncio.to_thread(
                jevlib.call, hub_dir, spec, subject=ident.user, surface=ident.surface,
                chat_id=_request_ctx.get_chat_id())
        except jevlib.JevError as exc:
            return f"[{TOOL_NAME} failed: {exc}]"
        except Exception as exc:  # noqa: BLE001 — never leak a traceback into chat
            log.exception("%s failed unexpectedly", TOOL_NAME)
            return f"[{TOOL_NAME} failed: unexpected {type(exc).__name__}; see the hub log]"
        return json.dumps({"answers": data["answers"], "model": data.get("model")})

    return [FunctionTool(
        name=TOOL_NAME,
        description=DESCRIPTION,
        params_json_schema=PARAMS_SCHEMA,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )]
