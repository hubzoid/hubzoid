"""Cards (MCP Apps): configuration, the result wrapper, the card host and the
MCP server's card metadata, on a small synthetic hub. No model, no network."""
from __future__ import annotations

import asyncio
import hashlib
import json
import textwrap
from pathlib import Path

import pytest

from hubzoid import _request_ctx, access
from hubzoid.apps import cards, config, kinds
from hubzoid.work_items.errors import WorkItemError

OWNER = "admin@localhost"

TOOLS = '''
import json
from agents import function_tool
from hubzoid import tool_classes

ROWS = [{"order": f"SAMPLE-00{i}", "quantity": i} for i in range(1, 8)]


@function_tool
def order_summary(cursor: str = "") -> str:
    "Synthetic orders, five a page."
    start = int(cursor) if cursor.isdigit() else 0
    nxt = str(start + 5) if start + 5 < len(ROWS) else None
    return json.dumps({"kind": "table", "title": "Orders | synthetic",
                       "columns": [{"key": "order", "label": "Order", "type": "string"},
                                   {"key": "quantity", "label": "Quantity", "type": "number"}],
                       "rows": ROWS[start:start + 5], "page": {"next_cursor": nxt, "total_rows": len(ROWS)},
                       "sources": [{"label": "Synthetic fixture"}]})


@function_tool
def broken_table() -> str:
    "Returns a table that breaks its own schema."
    return json.dumps({"kind": "table", "title": "x", "columns": [{"key": "a", "label": "A", "type": "number"}],
                       "rows": [{"a": "not a number"}], "page": {"next_cursor": None}, "sources": []})


@function_tool
def send_note(text: str) -> str:
    "Sends a note somewhere (an unclassified tool, so an external write)."
    return "sent"


tool_classes.declare(order_summary, "read")
tool_classes.declare(broken_table, "read")
'''


def _card(hub: Path, name: str, body: str) -> None:
    (hub / "cards").mkdir(exist_ok=True)
    (hub / "cards" / name).write_text(textwrap.dedent(body))


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for var in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_APPS_SANDBOX_ORIGIN", "HUBZOID_PUBLIC_URL",
                "MCP_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
    cards.store._active_cache.clear()
    cards._rate.clear()
    h = tmp_path / "cardhub"
    (h / "tools_local").mkdir(parents=True)
    (h / "AGENTS.md").write_text("---\nname: Card Hub\nmodel: claude-local\n---\nA hub with cards.\n")
    (h / "tools_local" / "orders.py").write_text(TOOLS)
    (h / config.FILE).write_text("version: 1\n")
    # Every tool call rechecks that its person may use the hub (access.apply).
    from hubzoid.access import store_for

    store_for(h).grant(OWNER, "cardhub", "use_hub")
    _card(h, "order-summary.yaml", """\
        version: 1
        id: order-summary
        title: Orders
        binding:
          tool: order_summary
        resource:
          builtin: table
        allowed_tools: [order_summary, send_note]
    """)
    _card(h, "broken.yaml", """\
        version: 1
        id: broken
        title: Broken
        binding: {tool: broken_table}
        resource: {builtin: table}
    """)
    return h


def _apply(hub: Path, expected=None) -> dict:
    return cards.apply(hub, expected=expected, actor="cli:test")


def _ident(user: str = OWNER):
    return access.Identity.make(user=user, surface="web")


async def _call(hub: Path, name: str, args: dict | None = None, user: str = OWNER, classes=None) -> dict:
    from hubzoid import mcp_server

    with _request_ctx.chat_scope("c1"):
        return await mcp_server.call_registry_tool(hub, name, args or {}, _ident(user), classes)


# -- configuration ----------------------------------------------------------------

def test_no_file_means_no_cards_and_an_untouched_registry(hub):
    (hub / config.FILE).unlink()
    registry = {"x": object()}
    assert cards.wrap(hub, registry) is registry
    assert config.load(hub).present is False


def test_errors_name_the_file_pointer_and_line(hub):
    _card(hub, "bad.yaml", "version: 1\nid: bad\ntitle: Bad\nbinding: {tool: nope}\nresource: {builtin: question}\n"
                           "colour: red\n")
    (hub / config.FILE).write_text("version: 1\nbuiltin_cards: [table, plan]\n")
    res = cards.validate(hub)
    by_code = {p.code: p for p in res.problems}
    assert by_code["card_unavailable"].file == config.FILE and by_code["card_unavailable"].line == 2
    unknown = next(p for p in res.problems if p.file == "cards/bad.yaml")
    assert unknown.pointer == "/colour" and unknown.line == 6 and "not a known key" in unknown.message
    assert not res.valid
    with pytest.raises(WorkItemError) as exc:
        _apply(hub)
    assert exc.value.code == "config_invalid" and exc.value.fields["errors"]


def test_custom_bundle_is_pinned_and_stays_inside_the_hub(hub, tmp_path):
    outside = tmp_path / "outside.html"
    outside.write_text("<!doctype html><p>x</p>")
    (hub / "cards" / "dist").mkdir(parents=True)
    (hub / "cards" / "dist" / "link.html").symlink_to(outside)
    page = hub / "cards" / "dist" / "own.html"
    page.write_text("<!doctype html><p>mine</p>")
    _card(hub, "escape.yaml", "version: 1\nid: escape\ntitle: E\nbinding: {tool: send_note}\n"
                              "resource: {file: cards/dist/link.html, sha256: '" + "0" * 64 + "'}\n")
    _card(hub, "own.yaml", "version: 1\nid: own\ntitle: O\nbinding: {tool: send_note}\n"
                           "resource: {file: cards/dist/own.html, sha256: '" + "1" * 64 + "'}\n")
    codes = {(p.file, p.code) for p in cards.validate(hub).problems}
    assert ("cards/escape.yaml", "resource_unsafe") in codes
    assert ("cards/own.yaml", "resource_hash_mismatch") in codes


def test_apply_is_compare_and_set(hub):
    first = _apply(hub)
    assert first["revision"] == 1 and first["digest"].startswith("sha256:")
    with pytest.raises(WorkItemError) as exc:
        _apply(hub)                       # still says "no revision yet"
    assert exc.value.code == "revision_conflict" and exc.value.fields["current_revision"] == 1
    with pytest.raises(WorkItemError) as exc:
        _apply(hub, expected=1)           # nothing changed
    assert exc.value.code == "unchanged"
    got = cards.get(hub)
    assert got["revision"] == 1 and {c["id"] for c in got["cards"]} == {"order-summary", "broken"}


# -- the result rules -------------------------------------------------------------

def test_table_rules_reject_what_a_card_cannot_show():
    base = {"kind": "table", "title": "t", "columns": [{"key": "n", "label": "N", "type": "number"},
                                                       {"key": "d", "label": "D", "type": "date"}],
            "rows": [], "page": {"next_cursor": None}, "sources": []}
    for rows, pointer in (([{"n": float("nan")}], "/rows/0/n"), ([{"x": 1}], "/rows/0/x"),
                          ([{"d": "2026-02-30"}], "/rows/0/d"), ([{"n": True}], "/rows/0/n")):
        with pytest.raises(kinds.Invalid) as exc:
            kinds.check("table", dict(base, rows=rows))
        assert exc.value.pointer == pointer
    with pytest.raises(kinds.Invalid):
        kinds.check("table", dict(base, rows=[{"n": 1}] * 3), rows_per_page=2)
    assert "No matching rows." in kinds.text(kinds.check("table", base))


def test_question_and_plan_wait_for_their_owners():
    assert set(kinds.UNAVAILABLE) == {"question", "plan"} and not set(kinds.BUILTIN) & set(kinds.UNAVAILABLE)


# -- presenting a result -------------------------------------------------------------

def test_a_bound_tool_returns_text_and_a_stored_card(hub):
    _apply(hub)
    out = asyncio.run(_call(hub, "order_summary"))
    assert out["text"].startswith("Orders | synthetic\n\n| Order | Quantity |")
    assert "| SAMPLE-005 | 5 |" in out["text"]
    assert 'More rows are available: call again with cursor "5".' in out["text"]     # the model can page too
    env = out["card"]
    assert env["kind"] == "table" and env["structured"]["page"]["next_cursor"] == "5"
    assert env["arguments"] == {} and env["allowed_tools"] == ["order_summary", "send_note"]
    view = cards.view(hub, env["instance_id"], OWNER)
    assert view["requester"] == OWNER and view["live"] and view["actions"] == ["order_summary", "send_note"]
    assert view["resource_uri"] == env["resource_uri"] and env["resource_uri"].startswith("ui://cardhub/order-summary/")
    # The result the model and every host see is the same checked data.
    assert view["result"] == env["structured"] and view["text"] == out["text"]


def test_a_result_that_breaks_its_card_keeps_the_tools_own_text(hub):
    _apply(hub)
    out = asyncio.run(_call(hub, "broken_table"))
    assert out["card"] is None and json.loads(out["text"])["rows"] == [{"a": "not a number"}]


def test_the_runtime_takes_the_card_of_its_own_call(hub):
    with _request_ctx.chat_scope("c1"):
        _request_ctx.record_card("same", {"instance_id": "i1", "tool": "a"}, call_id="call-1")
        _request_ctx.record_card("same", {"instance_id": "i2", "tool": "a"}, call_id="call-2")
        _request_ctx.record_card("hello", {"instance_id": "i3", "tool": "a"})
        # By call id first, whatever order the results come back in.
        assert _request_ctx.take_card("same", call_id="call-2", tool="a")["instance_id"] == "i2"
        # Without one (Claude's in-process tools), by tool and exact text: another tool's
        # identical text never takes it.
        assert _request_ctx.take_card("hello", tool="b") is None
        assert _request_ctx.take_card([{"type": "text", "text": "hello"}], tool="a")["instance_id"] == "i3"
        assert _request_ctx.take_card("same", call_id="call-1")["instance_id"] == "i1"
        assert _request_ctx.take_card("same") is None


def test_the_chat_part_stores_the_card(hub):
    from hubzoid.chat.stream import MessageBuilder
    from hubzoid.run_events import ToolCall, ToolResult

    env = {"instance_id": "i1", "card_id": "c", "kind": "table", "tool": "order_summary", "resource_uri": "ui://h/c/x/index.html",
           "text": "T", "structured": {"kind": "table"}}
    b = MessageBuilder(message_id="m", conversation_id="conv", show_tools=True)
    b.feed(ToolCall(id="t1", name="order_summary", args={}))
    chunks = b.feed(ToolResult(id="t1", name="order_summary", ok=True, card=env))
    part = b.snapshot()[0]
    assert part["result"]["status"] == "ok" and part["result"]["structuredContent"] == {"kind": "table"}
    assert part["result"]["content"] == [{"type": "text", "text": "T"}]
    assert part["result"]["_meta"]["hubzoid/card"]["instance_id"] == "i1"
    assert chunks[-1]["output"] == part["result"]


# -- the card host -------------------------------------------------------------------

def _dispatch(hub, inst, method, params=None, user=OWNER):
    from hubzoid import mcp_server

    async def call_tool(name, args, identity, classes):
        return await mcp_server.call_registry_tool(hub, name, args, identity, classes)

    return asyncio.run(cards.dispatch(hub, inst, user, method, params or {}, identity=_ident(user),
                                      call_tool=call_tool))


def test_every_card_request_is_rechecked(hub):
    _apply(hub)
    inst = asyncio.run(_call(hub, "order_summary"))["card"]["instance_id"]
    res = _dispatch(hub, inst, "mcp-apps/read-resource")
    assert res["mimeType"] == "text/html;profile=mcp-app" and res["html"].lower().startswith("<!doctype html")
    assert res["meta"]["csp"] == {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []}
    page2 = _dispatch(hub, inst, "tools/call", {"name": "order_summary", "arguments": {"cursor": "5"}})
    assert [r["order"] for r in page2["structuredContent"]["rows"]] == ["SAMPLE-006", "SAMPLE-007"]
    refusals = {
        "someone else": lambda: _dispatch(hub, inst, "mcp-apps/read-resource", user="eve@example.com"),
        "another resource": lambda: _dispatch(hub, inst, "resources/read", {"uri": "ui://cardhub/other/x/index.html"}),
        "a tool it may not call": lambda: _dispatch(hub, inst, "tools/call", {"name": "broken_table"}),
        "a write": lambda: _dispatch(hub, inst, "tools/call", {"name": "send_note", "arguments": {"text": "x"}}),
        "a hub id from the widget": lambda: _dispatch(hub, inst, "tools/call", {"name": "order_summary", "hub": "x"}),
        "a method cards don't get": lambda: _dispatch(hub, inst, "ui/message", {}),
    }
    codes = {}
    for what, fn in refusals.items():
        with pytest.raises(WorkItemError) as exc:
            fn()
        codes[what] = exc.value.code
    assert codes == {"someone else": "not_found", "another resource": "forbidden", "a tool it may not call": "forbidden",
                     "a write": "forbidden", "a hub id from the widget": "invalid_request",
                     "a method cards don't get": "method_not_allowed"}
    cards.revoke(hub, inst, OWNER, "test")
    with pytest.raises(WorkItemError) as exc:
        _dispatch(hub, inst, "mcp-apps/read-resource")
    assert exc.value.code == "revoked" and exc.value.status == 410


def test_every_kind_of_card_request_counts_toward_its_limit(hub):
    (hub / config.FILE).write_text("version: 1\nlimits:\n  calls_per_minute: 2\n")
    _apply(hub)
    inst = asyncio.run(_call(hub, "order_summary"))["card"]["instance_id"]
    _dispatch(hub, inst, "mcp-apps/read-resource")
    _dispatch(hub, inst, "tools/call", {"name": "hub_app_status"})
    for request in (lambda: _dispatch(hub, inst, "resources/list"),
                    lambda: cards.limit(hub, OWNER, cards.instance(hub, inst, OWNER))):   # the MCP status refresh
        with pytest.raises(WorkItemError) as exc:
            request()
        assert exc.value.code == "rate_limited" and exc.value.status == 429


def test_an_older_card_never_gains_a_tool_added_later(hub):
    _card(hub, "order-summary.yaml", "version: 1\nid: order-summary\ntitle: Orders\nbinding: {tool: order_summary}\n"
                                     "resource: {builtin: table}\nallowed_tools: [order_summary]\n")
    (hub / "cards" / "broken.yaml").unlink()
    _apply(hub)
    old = asyncio.run(_call(hub, "order_summary"))["card"]["instance_id"]
    _card(hub, "order-summary.yaml", "version: 1\nid: order-summary\ntitle: Orders\nbinding: {tool: order_summary}\n"
                                     "resource: {builtin: table}\nallowed_tools: [order_summary, broken_table]\n")
    _apply(hub, expected=1)
    new = asyncio.run(_call(hub, "order_summary"))["card"]["instance_id"]
    assert cards.view(hub, old, OWNER)["actions"] == ["order_summary"]
    assert cards.view(hub, new, OWNER)["actions"] == ["order_summary", "broken_table"]
    with pytest.raises(WorkItemError) as exc:
        _dispatch(hub, old, "tools/call", {"name": "broken_table"})
    assert exc.value.code == "forbidden"
    assert "content" in _dispatch(hub, new, "tools/call", {"name": "broken_table"})


def test_long_arguments_and_non_finite_numbers_never_break_a_card(hub):
    _apply(hub)
    out = asyncio.run(_call(hub, "order_summary", {"cursor": "x" * 30_000}))
    assert out["card"]["arguments"] is None                       # not kept, rather than cut into bad JSON
    assert cards.view(hub, out["card"]["instance_id"], OWNER)["result"]["kind"] == "table"
    page = hub / "cards" / "dist" / "nan.html"
    page.parent.mkdir(parents=True)
    page.write_text("<!doctype html><p>n</p>")
    sha = hashlib.sha256(page.read_bytes()).hexdigest()
    (hub / "tools_local" / "nan.py").write_text(
        "from agents import function_tool\nfrom hubzoid import tool_classes\n\n@function_tool\n"
        "def nan_result() -> str:\n    \"Returns NaN.\"\n    return '{\"title\": \"x\", \"value\": NaN}'\n\n"
        "tool_classes.declare(nan_result, 'read')\n")
    _card(hub, "nan.yaml", f"version: 1\nid: nan\ntitle: N\nbinding: {{tool: nan_result}}\n"
                           f"resource: {{file: cards/dist/nan.html, sha256: '{sha}'}}\n")
    _apply(hub, expected=1)
    from hubzoid import mcp_server

    mcp_server._dispatch_registry.clear()           # the tool file is new
    out = asyncio.run(_call(hub, "nan_result"))
    assert out["card"] is None and "NaN" in out["text"]


def test_the_api_candidate_is_checked_before_the_file_changes(hub):
    _apply(hub)
    before = (hub / config.FILE).read_text()
    with pytest.raises(WorkItemError) as exc:
        cards.apply(hub, expected=None, actor="cli:test", apps_doc={"version": 1, "enabled": False})
    assert exc.value.code == "revision_conflict" and (hub / config.FILE).read_text() == before
    with pytest.raises(WorkItemError):
        cards.apply(hub, expected=1, actor="cli:test", apps_doc={"version": 1, "colour": "red"})
    assert (hub / config.FILE).read_text() == before
    out = cards.apply(hub, expected=1, actor="cli:test", apps_doc={"version": 1, "enabled": False})
    assert out["revision"] == 2 and "enabled: false" in (hub / config.FILE).read_text()


def test_an_older_version_still_shows_but_cannot_call(hub):
    page = hub / "cards" / "dist" / "c.html"
    page.parent.mkdir(parents=True)

    def pin(html: str):
        page.write_text(html)
        sha = hashlib.sha256(html.encode()).hexdigest()
        _card(hub, "custom.yaml", f"version: 1\nid: custom\ntitle: C\nbinding: {{tool: order_summary}}\n"
                                  f"resource: {{file: cards/dist/c.html, sha256: {sha}}}\n"
                                  "allowed_tools: [order_summary]\n")

    (hub / "cards" / "order-summary.yaml").unlink()
    pin("<!doctype html><p>v1</p>")
    _apply(hub)
    inst = asyncio.run(_call(hub, "order_summary"))["card"]["instance_id"]
    pin("<!doctype html><p>v2</p>")
    _apply(hub, expected=1)
    view = cards.view(hub, inst, OWNER)
    assert not view["live"] and view["inactive_reason"] == "resource_changed" and view["actions"] == []
    assert _dispatch(hub, inst, "mcp-apps/read-resource")["html"] == "<!doctype html><p>v1</p>"
    with pytest.raises(WorkItemError) as exc:
        _dispatch(hub, inst, "tools/call", {"name": "order_summary"})
    assert exc.value.code == "card_inactive"


def test_the_sandbox_is_another_origin(hub):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from hubzoid.apps import routes

    assert routes.sandbox_origin("http://localhost:3180") == "http://127.0.0.1:3180"
    assert routes.sandbox_origin("http://127.0.0.1:3180") == "http://localhost:3180"
    assert routes.sandbox_origin("https://hub.example.com") is None
    router = APIRouter(prefix="/portal/api")
    routes.register(router, lambda: None, lambda actor, h: hub)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    url = "/portal/api/apps/sandbox/frame.html?origin=http%3A%2F%2F127.0.0.1%3A3180"
    ok = client.get(url, headers={"host": "localhost:3180"})
    assert ok.status_code == 200 and "frame-ancestors http://127.0.0.1:3180" in ok.headers["content-security-policy"]
    assert "connect-src 'none'" in ok.headers["content-security-policy"] and "set-cookie" not in ok.headers
    assert '"http://127.0.0.1:3180"' in ok.text
    for host in ("127.0.0.1:3180", "evil.example", "localhost:9999"):   # the chat's own origin, or any other host
        assert client.get(url, headers={"host": host}).status_code == 403


def test_a_configured_sandbox_serves_only_the_deployments_chat(monkeypatch):
    from hubzoid.apps.routes import sandbox_origin

    monkeypatch.setenv("HUBZOID_APPS_SANDBOX_ORIGIN", "https://cards.hub.example.com")
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.com")
    assert sandbox_origin("https://hub.example.com") == "https://cards.hub.example.com"
    assert sandbox_origin("http://localhost:3180") is None
    assert sandbox_origin("https://other.example.com") is None


# -- the MCP server --------------------------------------------------------------------

def test_the_mcp_server_publishes_cards_to_callers_who_may_use_them(hub, monkeypatch):
    from fastmcp import Client

    from hubzoid import mcp_server

    _apply(hub)
    who = {"ident": _ident()}
    monkeypatch.setattr(mcp_server, "_mcp_identity", lambda hub_dir: who["ident"])
    server, _ = mcp_server.build_server(hub)

    async def run():
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
            meta = tools["order_summary"].meta["ui"]
            assert meta["resourceUri"].startswith("ui://cardhub/order-summary/") and meta["visibility"] == ["model", "app"]
            assert tools["hub_app_status"].meta["ui"]["visibility"] == ["app"]
            read = await client.read_resource(meta["resourceUri"])
            assert read[0].mimeType == "text/html;profile=mcp-app"
            assert read[0].meta["ui"]["csp"]["connectDomains"] == []
            result = await client.call_tool("order_summary", {})
            assert result.structured_content["rows"][0]["order"] == "SAMPLE-001"
            assert result.content[0].text.startswith("Orders | synthetic")
            instance = result.meta["hubzoid/card"]["instance_id"]
            status = await client.call_tool("hub_app_status", {"instance_id": instance}, raise_on_error=False)
            assert status.is_error                     # a table has no live state
            cards.revoke(hub, instance, OWNER, "test")
            status = await client.call_tool("hub_app_status", {"instance_id": instance}, raise_on_error=False)
            assert status.is_error and "revoked" in status.content[0].text
            who["ident"] = access.ANONYMOUS
            with pytest.raises(Exception):
                await client.read_resource(meta["resourceUri"])

    asyncio.run(run())


def test_an_external_host_can_still_read_an_earlier_card_version(hub, monkeypatch):
    from fastmcp import Client

    from hubzoid import mcp_server

    page = hub / "cards" / "dist" / "c.html"
    page.parent.mkdir(parents=True)
    (hub / "cards" / "order-summary.yaml").unlink()

    def pin(html: str) -> str:
        page.write_text(html)
        sha = hashlib.sha256(html.encode()).hexdigest()
        _card(hub, "custom.yaml", f"version: 1\nid: custom\ntitle: C\nbinding: {{tool: order_summary}}\n"
                                  f"resource: {{file: cards/dist/c.html, sha256: '{sha}'}}\n")
        return sha

    old = pin("<!doctype html><p>v1</p>")
    _apply(hub)
    pin("<!doctype html><p>v2</p>")
    _apply(hub, expected=1)
    monkeypatch.setattr(mcp_server, "_mcp_identity", lambda hub_dir: _ident())
    server, _ = mcp_server.build_server(hub)

    async def run():
        async with Client(server) as client:
            read = await client.read_resource(f"ui://cardhub/custom/{old}/index.html")
            assert read[0].text == "<!doctype html><p>v1</p>"
            with pytest.raises(Exception):        # a version that was never this card's
                await client.read_resource(f"ui://cardhub/custom/{'0' * 64}/index.html")

    asyncio.run(run())
