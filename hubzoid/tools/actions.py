"""Agent tools for consequential actions, offered only as their write mode allows.

A hub that lets its agent send email or submit to an ERP builds the tool with
`action_tool(...)`. The hub's `work-items.yaml` decides what the agent sees:

* ``off`` (the default): no tool at all;
* ``draft``: a tool that saves a real draft in the target system, and says so;
* ``approve``: no tool in the agent's turn. Approval of exact content happens
  where the work parks and a person decides (`hub.operation.run` in a work-item
  workflow), never inside a model's turn.

So the agent can never send something the hub has not enabled, whatever it is
asked to do. The tool has the same name, schema and output on every runtime.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from agents import FunctionTool

from .. import redaction, tool_classes

log = logging.getLogger(__name__)


def action_tool(hub_dir, *, name: str, description: str, params_json_schema: dict, actions,
                draft: Callable[[dict], Any] | None, channel: str | None = None) -> FunctionTool | None:
    """The tool for one consequential action, or None when its mode hides it.

    `draft(arguments)` saves a real draft in the target system and returns a
    JSON-serializable result. Sending is never offered as an agent tool."""
    from ..work_items import config

    actions = [actions] if isinstance(actions, str) else list(actions)
    mode = config.mode_for(config.load(Path(hub_dir)), actions, channel)
    if mode != "draft" or draft is None:
        log.info("tool %s not offered: %s is %s", name, ", ".join(actions), mode)
        return None

    async def invoke(_ctx, args_json: str) -> str:
        import asyncio

        try:
            args = json.loads(args_json or "{}")
            result = await asyncio.to_thread(draft, args)
        except Exception as exc:  # noqa: BLE001 - the model gets a safe, plain error
            return json.dumps({"saved": False, "error": redaction.scrub_error(exc)})
        return json.dumps({"saved": True, "kind": "draft",
                           "note": "Saved as a draft for a person to review and send.",
                           "result": redaction.redact(result)}, default=str)

    tool = FunctionTool(name=name, description=description + " This saves a draft only; a person sends it.",
                        params_json_schema=params_json_schema, on_invoke_tool=invoke)
    return tool_classes.declare(tool, "external_write", action=",".join(actions))
