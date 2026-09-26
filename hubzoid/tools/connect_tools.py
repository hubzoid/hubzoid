"""Optional agent tool that starts a "connect my <app>" journey for the caller.

Off unless `HUBZOID_CONNECT_JOURNEY` is on (otherwise `make` returns no tool, so
no runtime sees it). The caller always comes from the trusted request identity:
the tool has no parameter naming a person. It returns a personal link to the
Hubzoid connection page, never a provider URL or a token. See
`hubzoid.connect_journey` for the journey and its checks.
"""
from __future__ import annotations

import json
import logging

from agents import FunctionTool

log = logging.getLogger("hubzoid.connect")

NAME = "connect_account"

_SCHEMA = {
    "type": "object",
    "properties": {
        "app": {
            "type": "string",
            "description": "The app to connect, for example \"gmail\".",
        },
        "reconnect": {
            "type": "boolean",
            "description": "Connect again even if already connected (for example after "
                           "access was revoked). Default false.",
        },
    },
    "required": ["app"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "Connect the current user's own account for an app (for example Gmail) so this "
    "agent can act as them. Use it when the user asks to connect an app, or when a "
    "tool says their account is not connected. Returns either a note that the app is "
    "already connected, or a personal link. Give the user that exact link. It works "
    "only for them while signed in, and expires in a few minutes."
)


def _reply(result: dict) -> str:
    if result["state"] == "connected":
        return (f"{result['label']} is already connected for this user. They can use it "
                "now. Call again with reconnect=true only if they ask to connect again.")
    minutes = max(1, int(result["expires_in"]) // 60)
    return (f"Send the user this personal link to connect {result['label']}: {result['url']}\n"
            f"It works only for {result['subject']} while signed in, and expires in "
            f"{minutes} minutes. After they approve access they will see a confirmation.")


def make(ctx) -> list:
    from .. import connect_journey

    if not connect_journey.enabled():
        return []
    hub_dir = ctx.hub_dir

    def start(app: str, reconnect: bool) -> dict:
        return connect_journey.start(hub_dir, app=app, reconnect=reconnect)

    async def invoke(_tool_ctx, input_str: str) -> str:
        import asyncio

        try:
            args = json.loads(input_str or "{}")
        except json.JSONDecodeError:
            return "[connect_account: arguments must be JSON with an 'app' name]"
        app = str(args.get("app") or "")
        reconnect = bool(args.get("reconnect", False))
        try:
            # Blocking DB and provider I/O. `to_thread` copies the context, so
            # the trusted identity and chat id reach the worker thread.
            result = await asyncio.to_thread(start, app, reconnect)
        except connect_journey.JourneyError as err:
            return err.message
        except Exception:  # noqa: BLE001 — never leak internals to the model
            log.exception("connect_account failed")
            return "The connection could not be started right now. Try again shortly."
        return _reply(result)

    return [FunctionTool(name=NAME, description=_DESCRIPTION, params_json_schema=_SCHEMA,
                         on_invoke_tool=invoke, strict_json_schema=False)]
