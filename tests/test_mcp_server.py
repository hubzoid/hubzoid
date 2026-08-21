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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def _mk_hub(tmp_path: Path, *, frontmatter_extra: str = "") -> Path:
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


def _call(app, body: dict, token: str | None = "sk-test") -> httpx.Response:
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


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db", groups=("clickup",))
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
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


def test_policy_mcp_surface_allowed_with_group():
    from hubzoid import access

    member = access.Identity.make(user="a@x", groups=["clickup"], surface="mcp")
    outsider = access.Identity.make(user="a@x", groups=[], surface="mcp")
    slack = access.Identity.make(user="a@x", groups=["clickup"], surface="slack")
    assert access.is_allowed(member, "clickup")[0]
    assert not access.is_allowed(outsider, "clickup")[0]
    assert not access.is_allowed(slack, "clickup")[0]


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
    assert "write_artifact" not in names


def test_tools_list_hides_restricted_for_nonmember(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db", groups=())  # no groups
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    names = {t["name"] for t in _result(_call(app, _rpc("tools/list")))["tools"]}
    assert "clickup_echo" not in names
    assert "read_knowledge" in names


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


def test_tools_call_restricted_fails_closed_for_nonmember(tmp_path, monkeypatch):
    """Even called by exact name (hidden from the list), the guard denies."""
    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db", groups=())
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    result = _result(_call(
        app, _rpc("tools/call", {"name": "clickup_echo", "arguments": {"text": "hi"}})
    ))
    assert "access denied" in result["content"][0]["text"]


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
            headers={**RPC_HEADERS, "Authorization": "Bearer sk-test"},
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
        env += "MCP_SERVER=true\n"
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
def test_access_group_gates_whole_surface(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db", groups=("clickup",))  # not in "irs"
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("MCP_ACCESS_GROUP", "irs")
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    # Valid key, wrong team: 401 before a single tool name leaks.
    assert _call(app, _rpc("tools/list")).status_code == 401


def test_access_group_admits_members(tmp_path, monkeypatch):
    hub = _mk_hub(tmp_path)
    db = _mk_owui_db(tmp_path / "webui.db", groups=("IRS",))  # case-insensitive
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("MCP_ACCESS_GROUP", "irs")
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    names = {t["name"] for t in _result(_call(app, _rpc("tools/list")))["tools"]}
    assert "read_knowledge" in names


def test_gateway_plan_carries_access_group(tmp_path):
    from hubzoid import gateway

    hub = _mk_gateway_hub(tmp_path, "alpha", 8100, mcp=True)
    (hub / ".env").write_text("BRIDGE_PORT=8100\nMCP_SERVER=true\nMCP_ACCESS_GROUP=irs\n")
    gp = gateway.plan([hub])
    assert gp.backends[0].mcp_access_group == "irs"


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
    """Two callers with different groups, interleaved: each tool run must see
    its own caller's identity (ContextVar scoping inside Tool.run)."""
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
                return await asyncio.gather(call("sk-alice", 0.3, 1), call("sk-bob", 0.05, 2))

    r_alice, r_bob = asyncio.run(go())
    assert "alice@x.io|['clickup']" in _result(r_alice)["content"][0]["text"]
    assert "bob@x.io|[]" in _result(r_bob)["content"][0]["text"]


# ---------------------------------------------------------------------------
# Roster (identity/access.csv) over MCP (unify-access, 0.8.1)
# ---------------------------------------------------------------------------
def _add_roster(hub: Path, email: str, groups: str) -> None:
    (hub / "identity").mkdir(exist_ok=True)
    (hub / "identity" / "access.csv").write_text(
        "phone,email,groups\n" f"919800000001,{email},{groups}\n"
    )


def test_roster_grants_restricted_tool_over_mcp(tmp_path, monkeypatch):
    """A coordinator granted 'clickup' in the roster (not in any OWUI group)
    can use the restricted tool over MCP — the WhatsApp/OWUI parity fix."""
    from hubzoid.access.resolver import reset_roster_cache

    reset_roster_cache()
    hub = _mk_hub(tmp_path)
    _add_roster(hub, "alice@example.com", "clickup")
    db = _mk_owui_db(tmp_path / "webui.db", groups=())  # no OWUI groups at all
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    result = _result(_call(
        app, _rpc("tools/call", {"name": "clickup_echo", "arguments": {"text": "hi"}})
    ))
    assert "clickup says: hi" in str(result)


def test_roster_cannot_open_mcp_front_door(tmp_path, monkeypatch):
    """MCP_ACCESS_GROUP is an OWUI-admin boundary: a roster entry naming that
    group must NOT admit a caller whose OWUI membership lacks it."""
    from hubzoid.access.resolver import reset_roster_cache

    reset_roster_cache()
    hub = _mk_hub(tmp_path)
    _add_roster(hub, "alice@example.com", "irs")  # roster claims the door group
    db = _mk_owui_db(tmp_path / "webui.db", groups=())  # OWUI does NOT grant irs
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("MCP_ACCESS_GROUP", "irs")
    from hubzoid import mcp_server

    app = mcp_server.build_mcp_app(hub)
    assert _call(app, _rpc("tools/list")).status_code == 401
