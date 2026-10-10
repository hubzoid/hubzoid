"""Agent tool that starts a "connect my <app>" journey for the caller, or lists
what they can connect in this agent.

On unless `HUBZOID_CONNECT_JOURNEY` is false (then `make` returns no tool, so no
runtime sees it). The caller always comes from the trusted request identity:
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
            "description": "The app to connect, for example \"gmail\". Leave it out to list "
                           "what this user can connect here.",
        },
        "reconnect": {
            "type": "boolean",
            "description": "Connect again even if already connected (for example after "
                           "access was revoked). Default false.",
        },
    },
    "additionalProperties": False,
}

_DESCRIPTION = (
    "Connect the current user's own account for an outside app (for example Gmail or "
    "Jira) so this agent can act as them. Use it when the user asks to connect an app, "
    "when a request needs an outside app and you have no tool for it, or when a tool "
    "says their account is not connected. Call it with no app to see what this user can "
    "connect here. Returns a note, or a personal link to give the user."
)


def _reply(result: dict) -> str:
    if result["state"] == "connected":
        return (f"{result['label']} is already connected for this user. They can use it "
                "now. Call again with reconnect=true only if they ask to connect again.")
    if result["state"] == "shared":
        return (f"{result['label']} uses one shared company account, so there is nothing for "
                "the user to connect. If its tools are missing, they should ask an "
                f"administrator for \"Use {result['label']}\".")
    return (f"Reply in one short line with this link: \"{result['label']} isn't connected "
            f"yet. [Connect {result['label']}]({result['url']})\". It works only for this "
            "user and expires soon.")


def _listing(rows: list[dict], page: str) -> str:
    if not rows:
        return "There is nothing this user can connect in this agent."
    words = {"connected": "connected", "none": "not connected", "expired": "expired, "
             "reconnect", "shared": "shared, nothing to connect", "unknown": "unknown"}
    items = "; ".join(f"{r['label']} ({r['app']}): {words.get(r['status'], r['status'])}"
                      for r in rows)
    return (f"What this user can connect here: {items}. To connect one, call again with its "
            f"app. They can manage connections at {page}.")


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
        if not app.strip():
            try:
                rows = await asyncio.to_thread(connect_journey.list_for, hub_dir)
            except connect_journey.JourneyError as err:
                return err.message
            except Exception:  # noqa: BLE001 — never leak internals to the model
                log.exception("connect_account listing failed")
                return "The connections could not be listed right now. Try again shortly."
            return _listing(rows, connect_journey.public_base() + "/portal/connections")
        try:
            # Blocking DB and provider I/O. `to_thread` copies the context, so
            # the trusted identity and chat id reach the worker thread.
            result = await asyncio.to_thread(start, app, reconnect)
        except connect_journey.JourneyError as err:
            if err.code != "unavailable":
                return err.message
            # A guessed name ("gmail" for the connector "mail"): say what exists.
            try:
                rows = await asyncio.to_thread(connect_journey.list_for, hub_dir)
            except Exception:  # noqa: BLE001 — the refusal alone still answers
                return err.message
            return err.message + " " + _listing(rows, connect_journey.public_base() + "/portal/connections")
        except Exception:  # noqa: BLE001 — never leak internals to the model
            log.exception("connect_account failed")
            return "The connection could not be started right now. Try again shortly."
        return _reply(result)

    return [FunctionTool(name=NAME, description=_DESCRIPTION, params_json_schema=_SCHEMA,
                         on_invoke_tool=invoke, strict_json_schema=False)]
