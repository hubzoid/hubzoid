"""Gateway access-control wiring.

The gateway path must carry the same identity-forwarding the single-hub path
does, or restricted tools are dead in gateway mode. This covers the
``ENABLE_FORWARD_USER_INFO_HEADERS`` drop-in: without it Open WebUI never
forwards the caller's email, the bridge derives an anonymous identity, and
every restricted tool is denied. The end-to-end audit line proves the flip.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from agents import function_tool
from agents.tool_context import ToolContext

from hubzoid import gateway, server
from hubzoid.access import audit as auditlib
from hubzoid.access import guard, identity_scope


@function_tool
def ornate_sales(store: str = "ALL") -> str:
    "Restricted Ornate sales lookup."
    return "sales:" + store


def _invoke(tool, **kwargs) -> str:
    args = json.dumps(kwargs)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


class _FakeRequest:
    """Minimal stand-in: _derive_identity only touches request.headers.get()."""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def _make_owui_db(hub_dir: Path) -> None:
    """OWUI's own DB at the single-hub default path, so resolve_groups finds it."""
    data = hub_dir / ".openwebui-data"
    data.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(data / "webui.db")
    con.executescript(
        '''
        CREATE TABLE "user" (id TEXT, email TEXT);
        CREATE TABLE "group" (id TEXT, name TEXT);
        CREATE TABLE group_member (id TEXT, group_id TEXT, user_id TEXT);
        INSERT INTO "user" VALUES ('u1', 'priya@x.com');
        INSERT INTO "group" VALUES ('g1', 'ornate');
        INSERT INTO group_member VALUES ('m1', 'g1', 'u1');
        '''
    )
    con.commit()
    con.close()


def _plan(hub: Path) -> gateway.GatewayPlan:
    return gateway.GatewayPlan(backends=(
        gateway.GatewayBackend(
            hub_dir=hub, slug="ornate", bridge_port=8000,
            api_key="k", model_label="ornate-agent",
        ),
    ))


def test_gateway_connection_env_forwards_user_info(tmp_path):
    """The drop-in: gateway connection env forwards user info like start() does."""
    env = _plan(tmp_path).connection_env()
    assert env["ENABLE_FORWARD_USER_INFO_HEADERS"] == "true"


def test_gateway_restricted_tool_audit_before_and_after(tmp_path, capsys):
    """End-to-end: the audit line flips from deny/anonymous to allow/group
    the moment OWUI forwards the email (which the drop-in enables)."""
    _make_owui_db(tmp_path)
    guarded = guard.guard_tool(ornate_sales, "ornate", tmp_path)

    # Case A — OWUI did NOT forward the email (pre-fix gateway): no header.
    ident_a = server._derive_identity({}, _FakeRequest(headers={}), tmp_path)
    with identity_scope(ident_a):
        out_a = _invoke(guarded, store="BLR")
    line_a = auditlib.read(tmp_path)[-1]

    # Case B — with ENABLE_FORWARD_USER_INFO_HEADERS=true OWUI forwards the
    # email; the bridge resolves the user's group from OWUI's DB.
    ident_b = server._derive_identity(
        {}, _FakeRequest(headers={"x-openwebui-user-email": "priya@x.com"}), tmp_path,
    )
    with identity_scope(ident_b):
        out_b = _invoke(guarded, store="BLR")
    line_b = auditlib.read(tmp_path)[-1]

    print("AUDIT (no header / pre-fix):        " + json.dumps(line_a))
    print("AUDIT (header forwarded / post-fix): " + json.dumps(line_b))

    # pre-fix: anonymous -> denied
    assert "access denied" in out_a.lower()
    assert (line_a["decision"], line_a["reason"], line_a["user"]) == ("deny", "anonymous", "anonymous")

    # post-fix: email -> group resolved -> allowed
    assert out_b == "sales:BLR"
    assert (line_b["decision"], line_b["reason"], line_b["user"]) == ("allow", "group", "priya@x.com")
