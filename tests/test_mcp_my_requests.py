"""``my_requests`` over MCP: what waits for the person, read only."""
from __future__ import annotations

import pytest

from tests.test_mcp_parity import X, hub  # noqa: F401 - fixture


@pytest.mark.asyncio
async def test_my_requests_lists_only_what_waits_for_me(hub):
    from hubzoid import mcp_parity
    from hubzoid.access import Identity, identity_scope
    from hubzoid.hitl import service

    me = service.Caller(account_id="ux", email=X, origin="mcp")
    other = service.Caller(account_id="uy", email="other@example.com", origin="mcp")
    service.create(hub, "parity", me, kind="approval", title="Reserve 4 units", tool="reserve_stock",
                   arguments={"sku": "A", "quantity": 4})
    service.create(hub, "parity", me, kind="question", title="Which region?", question="Which region?")
    service.create(hub, "parity", other, kind="approval", title="Someone else's", tool="t", arguments={})
    tool = mcp_parity.my_requests_tool(hub)
    with identity_scope(Identity.make(user=X, groups=[], surface="mcp", account_id="ux")):
        out = await tool.on_invoke_tool(None, "{}")
    assert "2 waiting for you" in out and "Reserve 4 units" in out and "Which region?" in out
    assert "Someone else's" not in out and "Approval:" in out and "Question:" in out
    with identity_scope(Identity.make(user="nobody@example.com", groups=[], surface="mcp", account_id="uz")):
        assert "Nothing is waiting" in await tool.on_invoke_tool(None, "{}")
    # Read only: nothing was decided.
    assert all(r["status"] == "pending" for r in service.list_for(hub, "parity", "ux", X))


def test_my_requests_is_offered_over_mcp(hub):
    from hubzoid import mcp_server, settings as settingslib

    registry, _ = mcp_server.build_registry(hub, settings=settingslib.load(hub))
    assert "my_requests" in registry
