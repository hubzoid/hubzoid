"""Hosted MCP server tests — registry shape, auth, access scoping, transport.

The MCP app is exercised over real Streamable HTTP (httpx ASGI transport,
stateless mode: one POST per JSON-RPC call), with a temp sqlite standing in
for OWUI's database. No model backend is involved anywhere — that is the
point of the feature.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"

RPC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

def _mk_owui_db(path: Path, *, email="alice@example.com", key="sk-test",
                groups=()) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        'CREATE TABLE "user"(id TEXT PRIMARY KEY, email TEXT);\n'
        'CREATE TABLE api_key(id TEXT, user_id TEXT, "key" TEXT, expires_at BIGINT);\n'
        'CREATE TABLE "group"(id TEXT PRIMARY KEY, name TEXT);\n'
        "CREATE TABLE group_member(group_id TEXT, user_id TEXT);"
    )
    con.execute('INSERT INTO "user" VALUES ("u1", ?)', (email,))
    con.execute('INSERT INTO api_key VALUES ("k1", "u1", ?, NULL)', (key,))
    for i, g in enumerate(groups):
        con.execute('INSERT INTO "group" VALUES (?, ?)', (f"g{i}", g))
        con.execute('INSERT INTO group_member VALUES (?, "u1")', (f"g{i}",))
    con.commit()
    con.close()
    return path


def _mk_hub(tmp_path: Path, *, frontmatter_extra: str = "", oauth_fixture: bool = True) -> Path:
    """A tiny hub: AGENTS.md, one knowledge doc, one restricted tool."""
    hub = tmp_path / "hub"
    (hub / "knowledge").mkdir(parents=True)
    (hub / "AGENTS.md").write_text(
        f"---\nname: mcphub\n{frontmatter_extra}---\n\nYou are MCPHub. Body instructions here.\n"
    )
    (hub / "knowledge" / "widgets.md").write_text("# Widgets\nThe widget count is 42.\n")
    (hub / "restricted").mkdir()
    (hub / "restricted" / "clickup.py").write_text(
        "from agents import function_tool\n\n"
        "@function_tool\n"
        "def clickup_echo(text: str) -> str:\n"
        '    """Echo, but only for the clickup group."""\n'
        "    return f'clickup says: {text}'\n"
    )
    from tests.mcp_credentials import seed
    from hubzoid.access import store_for
    if oauth_fixture:
        seed(hub)
    # Alice may use the hub; restricted tools need their own grant.
    store_for(hub).grant("alice@example.com", hub.name, "use_hub", actor="test")
    return hub


def _rpc(method: str, params: dict | None = None, id: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _result(resp: httpx.Response) -> dict:
    """Extract the JSON-RPC result from a JSON or SSE response."""
    assert resp.status_code == 200, resp.text
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        payload = resp.json()
    else:
        datas = [json.loads(l[len("data: "):]) for l in resp.text.splitlines()
                 if l.startswith("data: ")]
        assert datas, f"no SSE data in: {resp.text[:200]}"
        payload = datas[-1]
    assert "error" not in payload, payload
    return payload["result"]


def _call(app, body: dict, token: str | None = "oauth-test") -> httpx.Response:
    async def go():
        async with app.lifespan(app):
            transport = httpx.ASGITransport(app=app)
            headers = dict(RPC_HEADERS)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as c:
                return await c.post("/mcp", json=body, headers=headers)

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _clean_mcp_env(monkeypatch):
    """settings.load / gateway.plan run load_dotenv(override=True), which
    permanently leaks hub .env values (MCP_SERVER, MCP_ACCESS_GROUP) into
    this process's env — the exact bleed class the gateway pins against in
    production. Scrub before every test so no test inherits another's hub."""
    monkeypatch.delenv("MCP_SERVER", raising=False)
    monkeypatch.delenv("MCP_ACCESS_GROUP", raising=False)
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://hub.example/mcp")
    # MCP OAuth grants here belong to Open WebUI accounts: the legacy UI mode.


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from hubzoid.access import store_for

    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db")
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    store_for(hub).grant("alice@example.com", hub.name, "clickup", actor="test")
    return hub


@pytest.fixture
def mcp_app(hub):
    from hubzoid import mcp_server
    return mcp_server.build_mcp_app(hub)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------
def test_registry_excludes_chat_scoped_tools():
    from hubzoid import mcp_server

    registry, _ = mcp_server.build_registry(MINIMAL)
    for name in mcp_server.CHAT_SCOPED_TOOLS:
        assert name not in registry
    assert "read_knowledge" in registry
    assert "read_file" in registry


def test_registry_maps_restricted_permissions(hub):
    from hubzoid import mcp_server

    registry, perms = mcp_server.build_registry(hub)
    assert "clickup_echo" in registry
    assert perms == {"clickup_echo": "clickup"}


def test_policy_mcp_surface_allowed_with_a_grant():
    from hubzoid import access

    member = access.Identity.make(user="a@x", groups=[], surface="mcp")
    slack = access.Identity.make(user="a@x", groups=[], surface="slack")
    assert access.is_allowed(member, "clickup", can=lambda: True)[0]
    assert not access.is_allowed(member, "clickup", can=lambda: False)[0]
    assert not access.is_allowed(slack, "clickup", can=lambda: True)[0]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_mcp_rejects_missing_and_bad_tokens(mcp_app):
    assert _call(mcp_app, _rpc("tools/list"), token=None).status_code == 401
    assert _call(mcp_app, _rpc("tools/list"), token="sk-wrong").status_code == 401


def test_mcp_rejects_bridge_api_key(hub, monkeypatch):
    """BRIDGE_API_KEYS must be worthless on the MCP surface."""
    monkeypatch.setenv("BRIDGE_API_KEYS", "bridge-secret")
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    assert _call(app, _rpc("tools/list"), token="bridge-secret").status_code == 401


# ---------------------------------------------------------------------------
# Listing + access scoping
# ---------------------------------------------------------------------------
def test_tools_list_includes_restricted_for_member(mcp_app):
    result = _result(_call(mcp_app, _rpc("tools/list")))
    names = {t["name"] for t in result["tools"]}
    assert "read_knowledge" in names
    assert "clickup_echo" in names          # alice is in the clickup group
    # The file tools work over MCP (each person's own space), with upload_file.
    assert {"write_artifact", "read_upload", "upload_file"} <= names
    assert "record_feedback" not in names


def test_tools_list_hides_restricted_for_nonmember(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db", groups=())  # no groups
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    names = {t["name"] for t in _result(_call(app, _rpc("tools/list")))["tools"]}
    assert "clickup_echo" not in names
    assert "read_knowledge" in names


def _with_gated_builtin(monkeypatch, permission):
    """A built-in tool wrapped by the access guard (like the capability tools)."""
    from agents.tool import FunctionTool

    from hubzoid import tools as toolpkg
    from hubzoid.access.guard import guard_tool

    real = toolpkg.make_all

    def make_all(ctx):
        out = real(ctx)

        async def run(_ctx, _raw):
            return "gated ran"

        ft = FunctionTool(name="gated_builtin", description="A gated built-in.",
                          params_json_schema={"type": "object", "properties": {},
                                              "additionalProperties": True},
                          on_invoke_tool=run, strict_json_schema=False)
        out["gated_builtin"] = guard_tool(ft, permission, ctx.hub_dir)
        return out

    monkeypatch.setattr(toolpkg, "make_all", make_all)


def test_tools_list_hides_gated_builtin_without_the_grant(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db", groups=())  # no groups
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    _with_gated_builtin(monkeypatch, "clickup")
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    names = {t["name"] for t in _result(_call(app, _rpc("tools/list")))["tools"]}
    assert "gated_builtin" not in names
    assert "read_knowledge" in names


def test_tools_list_shows_gated_builtin_to_holders(hub, monkeypatch):
    _with_gated_builtin(monkeypatch, "clickup")  # alice is in the clickup group
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    names = {t["name"] for t in _result(_call(app, _rpc("tools/list")))["tools"]}
    assert "gated_builtin" in names


def test_tools_call_reads_knowledge(mcp_app):
    result = _result(_call(
        mcp_app, _rpc("tools/call", {"name": "read_knowledge", "arguments": {"name": "widgets"}})
    ))
    text = result["content"][0]["text"]
    assert "42" in text


def test_tools_call_restricted_allowed_for_member(mcp_app):
    result = _result(_call(
        mcp_app, _rpc("tools/call", {"name": "clickup_echo", "arguments": {"text": "hi"}})
    ))
    assert "clickup says: hi" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------
def test_initialize_carries_agents_md_body(mcp_app):
    result = _result(_call(mcp_app, _rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })))
    assert "Body instructions here" in result.get("instructions", "")
    assert result["serverInfo"]["name"] == "mcphub"


def test_mcp_instructions_frontmatter_wins(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path, frontmatter_extra="mcp_instructions: External-safe brief.\n")
    db = _mk_owui_db(tmp_path / "webui.db")
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    result = _result(_call(app, _rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })))
    assert result["instructions"] == "External-safe brief."


# ---------------------------------------------------------------------------
# Bridge integration (server.py mount)
# ---------------------------------------------------------------------------
class _StubRuntime:
    name = "stub"

    async def aopen(self):  # noqa: D102
        pass

    async def aclose(self):  # noqa: D102
        pass

    async def run(self, prompt):  # noqa: D102
        return "ok"

    def stream(self, prompt):  # noqa: D102
        raise NotImplementedError


@pytest.fixture
def bridge_env(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused")
    monkeypatch.setattr("hubzoid.server.runtime_lib.build", lambda hub_dir: _StubRuntime())
    return hub


def test_bridge_mounts_mcp_when_enabled(bridge_env, monkeypatch):
    from fastapi.testclient import TestClient
    from hubzoid.server import build_app

    monkeypatch.setenv("MCP_SERVER", "true")
    with TestClient(build_app()) as client:
        assert client.get("/healthz").status_code == 200          # bridge intact
        assert client.get("/v1/models").status_code == 401        # /v1 auth intact
        r = client.post("/mcp", json=_rpc("tools/list"), headers=RPC_HEADERS)
        assert r.status_code == 401                               # OWUI key required
        r = client.post(
            "/mcp", json=_rpc("tools/list"),
            headers={**RPC_HEADERS, "Authorization": "Bearer oauth-test"},
        )
        assert r.status_code == 200
        names = {t["name"] for t in _result(r)["tools"]}
        assert "read_knowledge" in names


def test_bridge_has_no_mcp_by_default(bridge_env, monkeypatch):
    from fastapi.testclient import TestClient
    from hubzoid.server import build_app

    monkeypatch.delenv("MCP_SERVER", raising=False)
    with TestClient(build_app()) as client:
        r = client.post("/mcp", json=_rpc("tools/list"), headers=RPC_HEADERS)
        assert r.status_code == 404


def test_bridge_key_does_not_open_mcp(bridge_env, monkeypatch):
    from fastapi.testclient import TestClient
    from hubzoid.server import build_app

    monkeypatch.setenv("MCP_SERVER", "true")
    with TestClient(build_app()) as client:
        r = client.post(
            "/mcp", json=_rpc("tools/list"),
            headers={**RPC_HEADERS, "Authorization": "Bearer dev"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Gateway planning
# ---------------------------------------------------------------------------
def _mk_gateway_hub(base: Path, name: str, port: int, *, mcp: bool) -> Path:
    hub = base / name
    hub.mkdir(parents=True)
    (hub / "AGENTS.md").write_text(f"---\nname: {name}\n---\nbody\n")
    env = f"BRIDGE_PORT={port}\n"
    if mcp:
        env += f"MCP_SERVER=true\nMCP_PUBLIC_URL=https://hub.example/b/{name}/mcp\n"
    (hub / ".env").write_text(env)
    return hub


def test_gateway_plan_flags_mcp_per_hub(tmp_path):
    from hubzoid import gateway

    a = _mk_gateway_hub(tmp_path, "alpha", 8100, mcp=True)
    b = _mk_gateway_hub(tmp_path, "beta", 8101, mcp=False)
    gp = gateway.plan([a, b])
    flags = {be.slug: be.mcp for be in gp.backends}
    assert flags == {"alpha": True, "beta": False}
    assert gp.any_mcp

    routes = gp.edge_routes()
    prefixes = {r["prefix"] for r in routes}
    assert "/b/alpha/mcp" in prefixes
    assert "/b/beta/mcp" not in prefixes
    mcp_route = next(r for r in routes if r["prefix"] == "/b/alpha/mcp")
    assert mcp_route["upstream"] == "http://127.0.0.1:8100"
    assert mcp_route["strip_prefix"] == "/b/alpha"


def test_gateway_plan_no_mcp_env_bleed(tmp_path, monkeypatch):
    """A truthy MCP_SERVER in the process env (leaked by a prior hub's .env
    load) must not flag hubs whose own .env does not set it."""
    from hubzoid import gateway

    monkeypatch.setenv("MCP_SERVER", "true")
    b = _mk_gateway_hub(tmp_path, "beta", 8101, mcp=False)
    gp = gateway.plan([b])
    assert not gp.backends[0].mcp
    assert not gp.any_mcp


# ---------------------------------------------------------------------------
# MCP_ACCESS_GROUP — the per-hub front door (gateway multi-tenancy)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Error paths + identity isolation
# ---------------------------------------------------------------------------
def test_raising_tool_surfaces_error_text(tmp_path, monkeypatch):
    """@function_tool catches exceptions inside the SDK ('non-fatal') and
    returns an error string, so the MCP client gets readable error text —
    same contract as the chat backends. (The adapter's is_error branch only
    fires for custom FunctionTools whose on_invoke actually raises.)"""
    hub = _mk_hub(tmp_path)
    (hub / "tools_local").mkdir()
    (hub / "tools_local" / "boom.py").write_text(
        "from agents import function_tool\n\n"
        "@function_tool\n"
        "def boom() -> str:\n"
        '    """Always raises."""\n'
        "    raise RuntimeError('kaboom')\n"
    )
    db = _mk_owui_db(tmp_path / "webui.db")
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    result = _result(_call(app, _rpc("tools/call", {"name": "boom", "arguments": {}})))
    assert "error" in result["content"][0]["text"].lower()


def test_denied_restricted_call_flagged_is_error(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db", groups=())
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    resp = _call(app, _rpc("tools/call", {"name": "clickup_echo", "arguments": {"text": "x"}}))
    ctype = resp.headers.get("content-type", "")
    payload = (resp.json() if ctype.startswith("application/json") else
               [json.loads(l[len("data: "):]) for l in resp.text.splitlines()
                if l.startswith("data: ")][-1])
    result = payload["result"]
    assert result["isError"] is True
    assert "access denied" in result["content"][0]["text"]


def test_concurrent_requests_do_not_bleed_identity(tmp_path, monkeypatch):
    """Two callers, interleaved: each tool run must see its own caller's
    identity (ContextVar scoping inside Tool.run)."""
    hub = _mk_hub(tmp_path)
    (hub / "tools_local").mkdir()
    (hub / "tools_local" / "whoami.py").write_text(
        "import asyncio\n"
        "from agents import function_tool\n"
        "from hubzoid import access\n\n"
        "@function_tool\n"
        "async def whoami(delay: float) -> str:\n"
        '    """Report the caller identity after a delay."""\n'
        "    await asyncio.sleep(delay)\n"
        "    ident = access.current_identity()\n"
        "    return f'{ident.user}|{sorted(ident.groups)}'\n"
    )
    db = tmp_path / "webui.db"
    con = sqlite3.connect(db)
    con.executescript(
        'CREATE TABLE "user"(id TEXT PRIMARY KEY, email TEXT);\n'
        'CREATE TABLE api_key(id TEXT, user_id TEXT, "key" TEXT, expires_at BIGINT);\n'
        'CREATE TABLE "group"(id TEXT PRIMARY KEY, name TEXT);\n'
        "CREATE TABLE group_member(group_id TEXT, user_id TEXT);"
    )
    con.executescript(
        'INSERT INTO "user" VALUES ("u1", "alice@x.io"), ("u2", "bob@x.io");\n'
        'INSERT INTO api_key VALUES ("k1","u1","sk-alice",NULL), ("k2","u2","sk-bob",NULL);\n'
        'INSERT INTO "group" VALUES ("g1", "clickup");\n'
        'INSERT INTO group_member VALUES ("g1", "u1");'
    )
    con.commit()
    con.close()
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    from hubzoid import mcp_server

    from tests.mcp_credentials import seed
    seed(hub, token="oauth-alice", email="alice@x.io", account_id="ua")
    seed(hub, token="oauth-bob", email="bob@x.io", account_id="ub")
    from hubzoid.access import store_for
    for who in ("alice@x.io", "bob@x.io"):
        store_for(hub).grant(who, hub.name, "use_hub", actor="test")
    app = mcp_server.build_mcp_app(hub)

    async def go():
        async with app.lifespan(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as c:
                async def call(token, delay, id):
                    return await c.post(
                        "/mcp",
                        json=_rpc("tools/call", {"name": "whoami", "arguments": {"delay": delay}}, id=id),
                        headers={**RPC_HEADERS, "Authorization": f"Bearer {token}"},
                    )
                # alice sleeps longer, so bob's request completes inside her window
                return await asyncio.gather(call("oauth-alice", 0.3, 1), call("oauth-bob", 0.05, 2))

    r_alice, r_bob = asyncio.run(go())
    assert _result(r_alice)["content"][0]["text"].startswith("alice@x.io|")
    assert _result(r_bob)["content"][0]["text"].startswith("bob@x.io|")


def test_authenticated_mcp_cannot_read_private_files(hub, mcp_app, monkeypatch):
    """An otherwise authorized MCP caller gets the same file wall as chat."""
    from hubzoid.tools import grep_data
    monkeypatch.setattr(grep_data.shutil, "which", lambda _: None)
    marker = "MCP_PRIVATE_CANARY_9214"
    (hub / ".env").write_text(marker)
    state = hub / ".openwebui-data"
    state.mkdir()
    (state / "webui.db").write_text(marker)
    (hub / "raw_data").mkdir()
    (hub / "raw_data" / "public.txt").write_text("public needle")
    (hub / "restricted" / "secret.txt").write_text(marker)
    for path in (".env", ".openwebui-data/webui.db"):
        result = _result(_call(mcp_app, _rpc("tools/call", {
            "name": "read_file", "arguments": {"path": path},
        })))
        text = result["content"][0]["text"]
        assert "refused" in text
        assert marker not in text
    result = _result(_call(mcp_app, _rpc("tools/call", {
        "name": "grep_data", "arguments": {"pattern": "needle|CANARY", "path": "."},
    })))
    text = result["content"][0]["text"]
    assert "public needle" in text
    assert marker not in text


# ---------------------------------------------------------------------------
# Connected apps: a person's Settings connectors on the hosted surface
# ---------------------------------------------------------------------------
ANA = "ana@example.org"
_UPSTREAM: list[str] = []   # every call that reached the fake app


def _totals(month: str) -> dict:
    _UPSTREAM.append("totals")
    return {"month": month, "orders": 7}


def _fails() -> str:
    _UPSTREAM.append("fails")
    from fastmcp.exceptions import ToolError

    raise ToolError("the app said no")


@pytest.fixture(scope="module")
def mail_url():
    from tests import connect_helpers as h

    def whoami(who):
        _UPSTREAM.append("whoami")
        return who or "nobody"

    with h.mcp_server("mail", {"tok-a": ANA}, {"whoami": whoami, "mail_search": lambda who: "mail"},
                      raw={"totals": _totals, "fails": _fails}) as url:
        yield url


@pytest.fixture
def connected(tmp_path, monkeypatch, mail_url):
    """``make(allow=None, dead=False, hub_tool=False)``: a hub where Ana has
    connected the mail app, signed in to the hosted surface as ``mcp-a``."""
    from tests import connect_helpers as h
    from tests.mcp_credentials import seed

    def make(allow=None, dead=False, hub_tool=False):
        _UPSTREAM.clear()
        hub = tmp_path / "apphub"
        (hub / "tools_local").mkdir(parents=True)
        (hub / "AGENTS.md").write_text("---\nname: apphub\n---\n\nThe app hub.\n")
        if hub_tool:
            (hub / "tools_local" / "mine.py").write_text(
                "from agents import function_tool\n\n@function_tool\n"
                "def my_mail__whoami() -> str:\n    \"\"\"The hub's own tool.\"\"\"\n    return 'hub'\n")
        servers = [{"id": "mail", "name": "Mail", "url": mail_url, **({"allow": allow} if allow else {})}]
        if dead:
            servers.append({"id": "dead", "name": "Dead", "url": "http://127.0.0.1:9/mcp"})
        db = tmp_path / "webui.db"
        h.seed_owui(db, users=[("ua", ANA)], secret="apps-secret", servers=servers)
        for s in servers:
            h.connect(db, user_id="ua", server_id=s["id"], access_token="tok-a")
        h.owui_env(monkeypatch, db, "apps-secret")
        h.isolated_store(tmp_path, monkeypatch)
        monkeypatch.delenv("HUBZOID_RESTRICTED_SURFACES", raising=False)
        h.grant(hub, ANA, permissions=("use_hub", "connector_mail", "connector_dead"))
        seed(hub, token="mcp-a", email=ANA, account_id="ua")
        from hubzoid import mcp_server

        return hub, mcp_server.build_mcp_app(hub)

    return make


def _ana(app, *calls):
    from tests import connect_helpers as h

    return asyncio.run(h.hosted(app, *(("mcp-a", m, p) for m, p in calls)))


def _tool_call(name, **arguments):
    return ("tools/call", {"name": name, "arguments": arguments})


def test_connected_app_results_keep_their_structure_and_error_flag(connected, caplog):
    from tests import connect_helpers as h

    _, app = connected()
    with caplog.at_level("DEBUG"):
        ok, bad = _ana(app, _tool_call("my_mail__totals", month="2026-11"), _tool_call("my_mail__fails"))
    assert ok["result"]["structuredContent"] == {"month": "2026-11", "orders": 7}
    assert not h.failed(ok)
    assert h.failed(bad) and "the app said no" in json.dumps(bad)
    assert "tok-a" not in caplog.text


def test_connected_app_allow_list_holds_on_list_and_call(connected):
    from tests import connect_helpers as h

    _, app = connected(allow=["whoami"])
    listed, called = _ana(app, ("tools/list", {}), _tool_call("my_mail__totals", month="x"))
    assert "my_mail__whoami" in h.tool_names(listed)
    assert "my_mail__totals" not in h.tool_names(listed)
    assert h.failed(called) and _UPSTREAM == []


def test_connected_app_call_rechecks_the_grant(connected):
    from hubzoid.access import store_for
    from tests import connect_helpers as h

    hub, app = connected()
    (listed,) = _ana(app, ("tools/list", {}))
    assert "my_mail__whoami" in h.tool_names(listed)
    store_for(hub).revoke(ANA, hub.name, "connector_mail", actor="test")
    (called,) = _ana(app, _tool_call("my_mail__whoami"))
    assert h.failed(called) and _UPSTREAM == []


def test_a_hub_tool_keeps_its_name_over_a_connected_app(connected):
    from tests import connect_helpers as h

    _, app = connected(hub_tool=True)
    listed, called = _ana(app, ("tools/list", {}), _tool_call("my_mail__whoami"))
    assert [t["name"] for t in listed["result"]["tools"]].count("my_mail__whoami") == 1
    assert h.text_of(called) == "hub" and _UPSTREAM == []


def test_connected_app_calls_are_audited_and_refused_without_a_row(connected, monkeypatch):
    from hubzoid.access import audit
    from tests import connect_helpers as h

    hub, app = connected()
    (ok,) = _ana(app, _tool_call("my_mail__whoami"))
    assert h.text_of(ok) == ANA
    rows = audit.read(hub, tool="my_mail__whoami")
    assert [(r["user"], r["surface"], r["decision"]) for r in rows] == [(ANA, "mcp", "allow")]

    _UPSTREAM.clear()
    monkeypatch.setattr(audit, "record", lambda *a, **k: False)
    (refused,) = _ana(app, _tool_call("my_mail__whoami"))
    assert h.failed(refused) and _UPSTREAM == []


def test_a_broken_app_does_not_break_the_tool_list(connected):
    from tests import connect_helpers as h

    _, app = connected(dead=True)
    (listed,) = _ana(app, ("tools/list", {}))
    names = h.tool_names(listed)
    assert "my_mail__whoami" in names and "list_knowledge" in names
    assert not {n for n in names if n.startswith("my_dead__")}


@pytest.mark.parametrize("rules", [
    "version: 1\nconnectors:\n  my_mail: {ask: always}\n",   # the server key chat evaluates
    "version: 1\ntools:\n  whoami: {ask: always}\n",          # the app's own tool name
])
def test_connected_app_calls_follow_the_hubs_approval_rules(connected, rules):
    from tests import connect_helpers as h

    hub, app = connected()
    (hub / "hitl.yaml").write_text(rules)
    (out,) = _ana(app, _tool_call("my_mail__whoami"))
    assert "waiting" in h.text_of(out).lower() and _UPSTREAM == []


def test_connected_app_catalog_reads_every_page():
    """The Agents SDK's list_tools reads only the first page of an app's tools."""
    from types import SimpleNamespace

    from hubzoid.mcp_connected_apps import _all_tools

    pages = {None: (["a", "b"], "p1")}
    pages.update({f"p{i}": ([f"t{i}"], f"p{i + 1}" if i < 12 else None) for i in range(1, 13)})

    class Session:
        async def list_tools(self, cursor=None):
            names, nxt = pages[cursor]
            return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in names], nextCursor=nxt)

    tools = asyncio.run(_all_tools(SimpleNamespace(session=Session())))
    assert [t.name for t in tools] == ["a", "b"] + [f"t{i}" for i in range(1, 13)]   # 13 pages

    looping = {None: (["a"], "same"), "same": (["b"], "same")}            # a cursor that never ends
    pages.clear(); pages.update(looping)
    assert [t.name for t in asyncio.run(_all_tools(SimpleNamespace(session=Session())))] == ["a", "b"]


def test_slow_connected_app_lookup_never_holds_up_the_list(monkeypatch):
    """A token refresh that hangs is cut off by the listing budget: no apps,
    answered in time."""
    import time

    from hubzoid import access
    from hubzoid import mcp_connected_apps as apps

    monkeypatch.setattr(apps, "BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(apps, "_servers", lambda *a: time.sleep(2) or ([], {}, set()))

    async def timed():   # inside the loop: asyncio.run itself waits for the thread to end
        started = time.monotonic()
        out = await apps.list_tools(Path("."), access.Identity.make(user="a@x.io", surface="mcp"), set())
        return out, time.monotonic() - started

    out, took = asyncio.run(timed())
    assert out == [] and took < 1.5


def test_connected_app_call_rechecks_the_grant_after_connecting(connected, monkeypatch):
    """A grant revoked between finding the caller's apps and forwarding the call
    still stops it, with a denial in the access log."""
    from hubzoid import access
    from hubzoid import mcp_connected_apps as apps
    from hubzoid.access import audit, store_for
    from tests import connect_helpers as h

    hub, app = connected()
    ana = access.Identity.make(user=ANA, groups=[], surface="mcp")
    found = apps._servers(hub, ana)                 # the lookup made before the grant goes
    store_for(hub).revoke(ANA, hub.name, "connector_mail", actor="test")
    monkeypatch.setattr(apps, "_servers", lambda *a: found)
    (out,) = _ana(app, _tool_call("my_mail__whoami"))
    assert h.failed(out) and _UPSTREAM == []
    assert [r["decision"] for r in audit.read(hub, tool="my_mail__whoami")] == ["deny"]


def test_connected_app_call_rereads_the_allow_list_before_forwarding(connected, monkeypatch):
    """An allow-list narrowed while the app was being reached stops the call."""
    import yaml

    from hubzoid import access
    from hubzoid import mcp_connected_apps as apps
    from hubzoid.connectors import registry
    from tests import connect_helpers as h

    hub, app = connected()
    ana = access.Identity.make(user=ANA, groups=[], surface="mcp")
    found = apps._servers(hub, ana)                       # the lookup made before the change
    path = registry.folder(hub) / "mail.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["tools"] = ["mail_search"]
    path.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(apps, "_servers", lambda *a: found)
    (out,) = _ana(app, _tool_call("my_mail__whoami"))
    assert h.failed(out) and "Use Mail" in h.text_of(out) and _UPSTREAM == []


def test_an_ambiguous_alias_is_not_forwarded_when_one_app_fails():
    """``my_acme__delete__item`` could be acme's ``delete__item`` or acme__delete's
    ``item``: with one of them down, it is still not called."""
    from hubzoid import access
    from hubzoid import mcp_connected_apps as apps
    from hubzoid.personal_mcp import PerUserServer
    from tests import connect_helpers as h

    hits = []
    with h.mcp_server("acme", {"tok": "ana"}, {}, raw={"delete__item": lambda: hits.append(1) or "deleted"}) as url:
        ok = PerUserServer(key="my_acme", url=url, headers={"Authorization": "Bearer tok"},
                           allowed_tools=None, server_id="acme", app="acme")
        down = PerUserServer(key="my_acme__delete", url="http://127.0.0.1:9/mcp", headers={},
                             allowed_tools=None, server_id="acme__delete", app="acme__delete")
        apps_servers = ([ok, down], {}, set())
        ident = access.Identity.make(user=ANA, groups=[], surface="mcp")
        orig = apps._servers
        apps._servers = lambda *a: apps_servers
        try:
            out = asyncio.run(apps.call(Path("."), ident, "my_acme__delete__item", {}))
        finally:
            apps._servers = orig
    assert out is None and hits == []


def test_a_cancelled_listing_stops_its_app_listings(monkeypatch):
    from types import SimpleNamespace

    from hubzoid import access
    from hubzoid import mcp_connected_apps as apps

    async def slow(_srv):
        await asyncio.sleep(30)

    monkeypatch.setattr(apps, "_fetch", slow)
    srv = SimpleNamespace(key="my_x", server_id="x", url="u", allowed_tools=None, headers={})

    async def go():
        task = asyncio.create_task(apps._list_all(Path("."), access.ANONYMOUS, [srv], 30))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(go()) == []


def test_read_only_hint_on_a_later_page_reaches_the_approval_gate(connected, monkeypatch):
    """Under ``ask: writes`` a read-only tool runs at once, wherever it is in the
    app's list: the gate gets every page's hints, not just page one's."""
    from types import SimpleNamespace

    from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

    from hubzoid import access, runtime
    from hubzoid import mcp_connected_apps as apps

    hub, _ = connected()
    (hub / "hitl.yaml").write_text("version: 1\nconnectors:\n  my_mail: {ask: writes}\n")
    schema = {"type": "object", "properties": {}}
    first = [Tool(name="whoami", inputSchema=schema)]
    second = [Tool(name="peek", inputSchema=schema, annotations=ToolAnnotations(readOnlyHint=True))]
    ran = []

    class Paged:
        name = "my_mail"

        def __init__(self):
            async def page(cursor=None):
                return SimpleNamespace(tools=second if cursor else first, nextCursor=None if cursor else "p2")
            self.session = SimpleNamespace(list_tools=page)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def list_tools(self, *a, **k):      # the SDK's: page one only
            return first

        async def call_tool(self, tool_name, arguments=None, *a, **k):
            ran.append(tool_name)
            return CallToolResult(content=[TextContent(type="text", text="peeked")])

    monkeypatch.setattr(runtime, "personal_mcp_server", lambda srv: Paged())
    ana = access.Identity.make(user=ANA, groups=[], surface="mcp")
    with access.identity_scope(ana):
        out = asyncio.run(apps.call(hub, ana, "my_mail__peek", {}))
    assert ran == ["peek"] and out.content[0].text == "peeked"


def test_a_running_call_stops_when_its_connection_is_revoked(tmp_path, monkeypatch):
    """Revoking an assistant's connection stops a call still running (a
    sub-agent's tool calls), not only the next request."""
    hub = _mk_hub(tmp_path)
    (hub / "tools_local").mkdir()
    (hub / "tools_local" / "probe.py").write_text(
        "from agents import function_tool\n\n"
        "@function_tool\n"
        "def revoke_then_check() -> str:\n"
        '    """Revoke this connection mid-call, then ask what the next tool call would see."""\n'
        "    from pathlib import Path\n"
        "    from hubzoid.access.evaluator import check_run\n"
        "    from hubzoid.access.identity import current_identity\n"
        "    from hubzoid.mcp_oauth_store import OAuthStore\n"
        f"    hub = Path({str(hub)!r})\n"
        "    store = OAuthStore(hub, 'https://hub.example/mcp')\n"
        "    with store.engine.begin() as c:\n"
        "        for g in store.grants(c):\n"
        "            store.revoke(c, g['id'])\n"
        "    return 'next call: ' + (check_run(hub, current_identity()).reason or 'allowed')\n")
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    result = _result(_call(app, _rpc("tools/call", {"name": "revoke_then_check", "arguments": {}})))
    assert result["content"][0]["text"] == "next call: credential_ended"
