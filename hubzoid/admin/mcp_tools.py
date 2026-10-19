"""Read-only admin tools for hosted MCP: agent health, eval status and recent
denials, for people who administer an agent.

They need ``admin.diagnostics.read`` on the hub (``hubzoid.control.authz``).
Without it the tools are hidden from ``tools/list`` and a direct call returns
an access denial. They are added to the MCP surface only, never to a chat
model's tools, and never return secret values, secret names, secret-store
paths or credential fingerprints.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

CAPABILITY = "admin.diagnostics.read"
TOOL_NAMES = ("agent_health", "eval_status", "recent_denials")


def _principal_for_email(hub_dir: Path, email: str):
    from ..access.identity import normalize
    from ..control.authz import Principal
    from ..access.service import AccessService, Actor

    scope = AccessService(hub_dir).scope(Actor(normalize(email), "mcp", "session"))
    return Principal(id=email, email=normalize(email), org_admin=bool(scope.org_admin), hubs=tuple(scope.hubs))


def allowed(hub_dir: Path, email: str | None) -> bool:
    if not email:
        return False
    hub = Path(hub_dir).name.lower()
    try:
        return _principal_for_email(Path(hub_dir), email).can(CAPABILITY, hub)
    except Exception:  # noqa: BLE001 - fail closed
        return False


def _denied() -> str:
    return f"[access denied: these admin tools need the {CAPABILITY} permission on this agent]"


def make(hub_dir: Path) -> list:
    """FunctionTools (the SDK's canonical tool shape) bound to this hub."""
    from agents import FunctionTool

    from ..access.identity import current_identity

    hub_dir = Path(hub_dir)
    hub = hub_dir.name.lower()

    def caller() -> str | None:
        ident = current_identity()
        return getattr(ident, "user", None)

    async def health(_ctx, _args: str) -> str:
        if not allowed(hub_dir, caller()):
            return _denied()
        from .diagnostics import CHECKS, scrub

        out = []
        for c in CHECKS:
            if c.profile != "passive":
                continue
            try:
                r = c.fn(hub_dir)
                out.append({"check": c.id, "status": r.status, "summary": scrub(r.summary),
                            "next_step": r.remediation})
            except Exception as exc:  # noqa: BLE001
                out.append({"check": c.id, "status": "fail", "summary": f"The check raised {type(exc).__name__}."})
        return json.dumps({"agent": hub, "checks": out})

    async def evals(_ctx, _args: str) -> str:
        if not allowed(hub_dir, caller()):
            return _denied()
        from ..evals import report as eval_report

        try:
            page = eval_report.summaries(hub_dir, offset=0, limit=3)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"agent": hub, "error": f"Eval history unavailable ({type(exc).__name__})."})
        return json.dumps({"agent": hub, "recent": page}, default=str)

    async def denials(_ctx, args: str) -> str:
        if not allowed(hub_dir, caller()):
            return _denied()
        try:
            hours = int(json.loads(args or "{}").get("hours") or 24)
        except (ValueError, AttributeError):
            hours = 24
        hours = max(1, min(hours, 24 * 30))
        from sqlalchemy import text

        from .. import db

        since = time.time() - hours * 3600
        with db.operational_engine(hub_dir).connect() as conn:
            rows = conn.execute(text(
                "SELECT ts, subject, tool, reason, surface FROM hz_access_decisions "
                "WHERE decision='deny' AND hub=:h AND ts >= :s ORDER BY ts DESC LIMIT 50"),
                {"h": hub, "s": since}).mappings().all()
        return json.dumps({"agent": hub, "hours": hours, "denials": [dict(r) for r in rows]}, default=str)

    def tool(name, description, fn, params):
        ft = FunctionTool(name=name, description=description, params_json_schema=params, on_invoke_tool=fn)
        ft.hubzoid_capability = CAPABILITY  # read by the MCP list filter
        return ft

    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    return [
        tool("agent_health", "Read-only health of this agent: storage, model sign-in, schedules, "
             "connectors and configuration, each with a status and the next step.", health, empty),
        tool("eval_status", "The latest eval run of this agent.", evals, empty),
        tool("recent_denials", "Recent refused tool calls on this agent (who, which tool, when).", denials,
             {"type": "object", "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 720}},
              "additionalProperties": False}),
    ]
