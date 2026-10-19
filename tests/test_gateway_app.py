"""`hubzoid gateway` in the web app mode: N bridges and one edge, no Open
WebUI; the edge's routes (contract section 2); agents and branding across the
deployment; and the edge's behaviour by mode (no Open WebUI rewrites in the
web app mode, all of them intact in Open WebUI mode)."""
from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from starlette.testclient import TestClient as StarletteClient
from typer.testing import CliRunner

import hubzoid.access as access
import hubzoid.db as db
from hubzoid import cli, deployment, edge, gateway, secretbox, webapp_gateway
from hubzoid.auth import AuthUser

_ENV = ("HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH", "HUBZOID_SECRET_KEY", "HUBZOID_DEPLOYMENT",
        "HUBZOID_OPERATIONAL_DB", "DATABASE_URL", "HUBZOID_DBOS_DB", "HUBZOID_GATEWAY_BRANDING",
        "HUBZOID_PUBLIC_URL", "WEBUI_URL", "HUBZOID_ALLOWED_ORIGINS", "HUBZOID_DISABLE_EDGE",
        "HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK", "HUBZOID_HIDE_OWUI_USERS", "PORT",
        "HUBZOID_LOCK_OWUI_ACCESS_UI", "HUBZOID_HOST", "AWS_SECRET_NAME", "HUBZOID_ADMIN_EMAIL",
        "HUBZOID_GATEWAY_ADMIN_EMAIL", "HUBZOID_GATEWAY_ADMIN_PASSWORD", "WEBUI_ADMIN_EMAIL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    access._stores.clear()
    secretbox.reset_cache()
    yield
    access._stores.clear()
    secretbox.reset_cache()


def _hub(root: Path, name: str, port: int, env: str = "", agent_name: str | None = None) -> Path:
    hub = root / name
    hub.mkdir(parents=True)
    (hub / "AGENTS.md").write_text(f"---\nname: {agent_name or name}\ndescription: The {name} agent\n"
                                   f"suggestions:\n  - Hello {name}\n---\nbody")
    (hub / ".env").write_text(f"BRIDGE_PORT={port}\nMODEL=claude-local\n{env}")
    return hub


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------
def test_plan_routes_hub_scoped_web_app_calls(tmp_path):
    sales, support = _hub(tmp_path, "sales", 3611), _hub(tmp_path, "support", 3612)
    gp = gateway.plan([sales, support])
    routes = {r["prefix"]: r for r in gp.edge_routes()}
    # The hub-scoped API goes by hub key, with its path unchanged.
    for key, port in (("sales", 3611), ("support", 3612)):
        assert routes[f"/api/hubs/{key}"] == {"prefix": f"/api/hubs/{key}",
                                               "upstream": f"http://127.0.0.1:{port}"}
    for slug, port in (("sales", 3611), ("support", 3612)):
        for part in ("api", "artifacts", "branding", "portal/api"):
            r = routes[f"/b/{slug}/{part}"]
            assert r["upstream"] == f"http://127.0.0.1:{port}" and r["strip_prefix"] == f"/b/{slug}"


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------
class _Run:
    def __init__(self, monkeypatch, tmp_path):
        self.popen = []
        self.tmp = tmp_path

        def fake_popen(cmd, env=None, **kw):
            self.popen.append({"cmd": cmd, "env": dict(env or {})})
            proc = MagicMock()
            proc.poll.return_value = None
            return proc

        monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
        monkeypatch.setattr(cli, "_ensure_port_available", lambda *a: None)
        monkeypatch.setattr(cli, "_wait_any", lambda procs, **k: None)
        monkeypatch.setattr(cli, "_stop_groups", lambda procs, **k: list(procs))
        monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)

    def invoke(self, *args):
        return CliRunner().invoke(cli.app, ["gateway", *map(str, args)], catch_exceptions=False)

    def bridges(self):
        return [c for c in self.popen if c["cmd"][1:4] == ["-m", "hubzoid", "run"]]

    def edge(self):
        return next(c for c in self.popen if "hubzoid.edge:_factory" in c["cmd"])


def test_gateway_runs_bridges_and_an_edge_without_open_webui(tmp_path, monkeypatch):
    sales, support = _hub(tmp_path, "sales", 3611), _hub(tmp_path, "support", 3612)
    gw = tmp_path / "gw"
    run = _Run(monkeypatch, tmp_path)
    result = run.invoke(sales, support, "--port", "3610", "--data-dir", gw,
                        "--public-url", "https://hub.example.org", "--name", "Acme")
    assert result.exit_code == 0, result.output
    assert "Open WebUI" not in result.output

    bridges = run.bridges()
    assert [b["cmd"][4] for b in bridges] == [str(sales), str(support)]
    for b, port in zip(bridges, (3611, 3612)):
        assert b["cmd"][5:] == ["--no-ui", "--bridge-port", str(port)]
        env = b["env"]
        assert env["HUBZOID_UI"] == "hubzoid" and env["HUBZOID_AUTH"] == "false"
        assert env["HUBZOID_GATEWAY"] == "1" and env["HUBZOID_HOST"] == "127.0.0.1"
        assert env["HUBZOID_OPERATIONAL_DB"] == f"sqlite:///{gw / 'hubzoid-operational.db'}"
        assert "HUBZOID_OWUI_DB" not in env
    assert bridges[0]["env"]["HUBZOID_PUBLIC_URL"] == "https://hub.example.org/b/sales"

    edge_run = run.edge()
    assert edge_run["cmd"][-4:] == ["--port", "3610", "--log-level", "info"]
    env = edge_run["env"]
    assert env["HUBZOID_UI"] == "hubzoid"
    assert env["HUBZOID_EDGE_DEFAULT"] == "http://127.0.0.1:3611"
    assert json.loads(env["HUBZOID_EDGE_DEFAULT_FALLBACKS"]) == ["http://127.0.0.1:3612"]
    assert env["HUBZOID_EDGE_PUBLIC_SCHEME"] == "https"
    assert env["HUBZOID_DEPLOYMENT"] == str(gw / "deployment.json")
    prefixes = {r["prefix"] for r in json.loads(env["HUBZOID_EDGE_ROUTES"])}
    assert {"/b/sales/api", "/b/sales/branding", "/b/sales/artifacts",
            "/b/support/api", "/b/support/branding", "/b/support/artifacts"} <= prefixes

    manifest = json.loads((gw / "deployment.json").read_text())
    assert manifest["ui_mode"] == "hubzoid" and manifest["auth"] is False
    assert manifest["allowed_origins"] == ["https://hub.example.org"]
    assert manifest["name"] == "Acme" and "owui_url" not in manifest
    assert manifest["branding_dir"] == str(sales / "branding")   # the first hub by default
    assert [h["slug"] for h in manifest["hubs"]] == ["sales", "support"]
    assert [h["model_id"] for h in manifest["hubs"]] == ["sales", "support"]

    # One deployment key, next to the manifest, shared by every bridge.
    key = gw / "secret.key"
    assert key.is_file() and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert secretbox.key_path(sales) == key == secretbox.key_path(support)


def test_gateway_branding_folder_is_recorded(tmp_path, monkeypatch):
    sales, support = _hub(tmp_path, "sales", 3611), _hub(tmp_path, "support", 3612)
    gw = tmp_path / "gw"
    (gw / "branding").mkdir(parents=True)
    (gw / "branding" / "logo.png").write_bytes(b"\x89PNG")
    run = _Run(monkeypatch, tmp_path)
    assert run.invoke(sales, support, "--data-dir", gw).exit_code == 0
    assert json.loads((gw / "deployment.json").read_text())["branding_dir"] == str(gw / "branding")
    monkeypatch.setenv("HUBZOID_GATEWAY_BRANDING", "support")
    assert run.invoke(sales, support, "--data-dir", gw).exit_code == 0
    assert json.loads((gw / "deployment.json").read_text())["branding_dir"] == str(support / "branding")


@pytest.mark.parametrize("hub_env,gateway_env,needle", [
    ("HUBZOID_AUTH=false\n", {"HUBZOID_AUTH": "true"}, "HUBZOID_AUTH=false"),
    ("HUBZOID_UI=openwebui\n", {}, "HUBZOID_UI=openwebui"),
    ("HUBZOID_SECRET_KEY=abc\n", {}, "HUBZOID_SECRET_KEY"),
])
def test_a_hub_that_would_split_the_deployment_stops_the_gateway(tmp_path, monkeypatch, hub_env,
                                                                  gateway_env, needle):
    sales = _hub(tmp_path, "sales", 3611)
    support = _hub(tmp_path, "support", 3612, env=hub_env)
    for k, v in gateway_env.items():
        monkeypatch.setenv(k, v)
    run = _Run(monkeypatch, tmp_path)
    result = run.invoke(sales, support, "--data-dir", tmp_path / "gw")
    assert result.exit_code == 2 and needle in result.output
    assert run.popen == []


def test_open_webui_sign_in_names_stop_the_gateway_until_renamed(tmp_path, monkeypatch):
    sales = _hub(tmp_path, "sales", 3611, env="WEBUI_AUTH=true\n")
    support = _hub(tmp_path, "support", 3612, env="WEBUI_AUTH=false\n")
    run = _Run(monkeypatch, tmp_path)
    result = run.invoke(sales, support, "--data-dir", tmp_path / "gw")
    assert result.exit_code == 2 and "WEBUI_AUTH" in result.output and "HUBZOID_AUTH" in result.output
    assert run.popen == []
    for hub in (sales, support):  # renamed: the hubs drop the Open WebUI name
        env = hub / ".env"
        env.write_text("".join(l for l in env.read_text().splitlines(True) if not l.startswith("WEBUI_AUTH")))
    monkeypatch.delenv("WEBUI_AUTH", raising=False)  # a fresh process would not have it
    monkeypatch.setenv("HUBZOID_AUTH", "true")   # decided once, for everyone
    assert run.invoke(sales, support, "--data-dir", tmp_path / "gw").exit_code == 0
    assert all(b["env"]["HUBZOID_AUTH"] == "true" for b in run.bridges())


def test_no_sign_in_on_a_network_address_is_refused(tmp_path, monkeypatch):
    sales = _hub(tmp_path, "sales", 3611)
    run = _Run(monkeypatch, tmp_path)
    result = run.invoke(sales, "--host", "0.0.0.0", "--data-dir", tmp_path / "gw")
    assert result.exit_code == 2 and "HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK" in result.output
    monkeypatch.setenv("HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK", "true")
    assert run.invoke(sales, "--host", "0.0.0.0", "--data-dir", tmp_path / "gw").exit_code == 0
    monkeypatch.delenv("HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    assert run.invoke(sales, "--host", "0.0.0.0", "--data-dir", tmp_path / "gw").exit_code == 0

@pytest.fixture
def deployment_hubs(tmp_path, monkeypatch):
    """Two hubs registered in one manifest, one shared store; sales is managed
    in the Console, support still on legacy access."""
    sales, support = _hub(tmp_path, "sales", 3611), _hub(tmp_path, "support", 3612)
    (sales / "branding").mkdir()
    (sales / "branding" / "Logo.PNG").write_bytes(b"sales-logo")
    gw = tmp_path / "gw"
    (gw / "branding").mkdir(parents=True)
    (gw / "branding" / "logo.svg").write_bytes(b"<svg/>")
    (gw / "branding" / ".secret").write_text("no")
    eng = create_engine(f"sqlite:///{tmp_path / 'op.db'}")
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    deployment.save(gw / "deployment.json",
                    hubs=[dict(key=h.name, name=h.name.title(), path=str(h), model_id=f"{h.name}-agent",
                               slug=h.name) for h in (sales, support)],
                    operational_url=f"sqlite:///{tmp_path / 'op.db'}", owui_db="",
                    ui_mode="hubzoid", auth=True, name="Acme", branding_dir=str(gw / "branding"))
    gs = access.store_for(sales)
    gs.bootstrap(["boss@example.org"])
    return sales, support, gw, gs


def _app_for(hub, who):
    def resolve(_request, _hub_dir):
        return AuthUser(id="u", email=who["email"], role="user") if who["email"] else None

    app = FastAPI()
    webapp_gateway.mount(app, hub, model_label="ignored-in-a-gateway")
    return app, resolve


def test_agents_span_the_deployment_per_person(deployment_hubs, monkeypatch):
    sales, support, _gw, gs = deployment_hubs
    who = {"email": "ann@example.org"}
    app, resolve = _app_for(sales, who)
    monkeypatch.setattr("hubzoid.auth.sessions.resolve", resolve)
    c = TestClient(app)

    gs.grant("ann@example.org", "support", "use_hub", actor="boss@example.org")
    body = c.get("/api/agents").json()   # ann has no entry to sales yet
    assert [a["id"] for a in body["agents"]] == ["support-agent"]
    assert body["agents"][0]["api_base"] == "/b/support"
    assert body["default_agent"] == "support-agent"

    gs.grant("ann@example.org", "sales", "use_hub", actor="boss@example.org")
    agents = c.get("/api/agents").json()["agents"]
    assert [a["id"] for a in agents] == ["sales-agent", "support-agent"]
    sales_card = agents[0]
    assert sales_card == {
        "id": "sales-agent", "name": "sales", "description": "The sales agent",
        "suggestions": ["Hello sales"], "hub": "sales", "api_base": "/b/sales",
        "avatar_url": "/b/sales/branding/Logo.PNG"}
    assert agents[1]["avatar_url"] is None

    gs.suspend("ann@example.org", actor="boss@example.org")
    assert c.get("/api/agents").json() == {"agents": [], "default_agent": None}


def test_agents_fail_closed(deployment_hubs, monkeypatch):
    sales, *_ = deployment_hubs
    who = {"email": "ann@example.org"}
    app, resolve = _app_for(sales, who)
    monkeypatch.setattr("hubzoid.auth.sessions.resolve", resolve)

    def boom(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(access, "store_for", boom)
    r = TestClient(app).get("/api/agents")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "access_unavailable"
    who["email"] = None
    assert TestClient(app).get("/api/agents").status_code == 401


def test_branding_is_the_gateways_and_each_hubs_under_its_prefix(deployment_hubs, monkeypatch):
    sales, support, gw, _gs = deployment_hubs
    app, resolve = _app_for(sales, {"email": "ann@example.org"})
    c = TestClient(app)
    assert c.get("/api/branding").json() == {
        "name": "Acme", "logo_url": "/branding/logo.svg", "favicon_url": "/branding/logo.svg",
        "custom_css_url": None, "manifest": None}
    assert c.get("/branding/logo.svg").content == b"<svg/>"
    assert c.get("/branding/.secret").status_code == 404
    assert c.get("/branding/..%2F..%2Fdeployment.json").status_code == 404
    assert c.get("/branding/missing.png").status_code == 404
    # Forwarded by the edge from /b/sales/...: this hub's own branding.
    hub_scoped = {"X-Forwarded-Prefix": "/b/sales"}
    assert c.get("/api/branding", headers=hub_scoped).json()["logo_url"] == "/b/sales/branding/Logo.PNG"
    assert c.get("/branding/logo.png", headers=hub_scoped).content == b"sales-logo"
    # Another hub's prefix is not this bridge's: the deployment's branding.
    assert c.get("/branding/logo.svg", headers={"X-Forwarded-Prefix": "/b/support"}).content == b"<svg/>"


def test_single_hub_keeps_its_own_agent_and_branding(tmp_path, monkeypatch):
    hub = _hub(tmp_path, "solo", 3611)
    (hub / "branding").mkdir()
    (hub / "branding" / "favicon.png").write_bytes(b"fav")
    eng = create_engine(f"sqlite:///{tmp_path / 'op.db'}")
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    from hubzoid.access import store_for

    store_for(hub).grant("admin@localhost", "solo", "use_hub", actor="t")  # provisioned at start
    app = FastAPI()
    webapp_gateway.mount(app, hub, model_label="solo-label")
    c = TestClient(app)   # local mode: the local owner
    body = c.get("/api/agents").json()
    assert body["agents"][0]["id"] == "solo-label" and body["agents"][0]["api_base"] == ""
    assert body["agents"][0]["avatar_url"] == "/branding/favicon.png"
    assert c.get("/api/branding").json()["name"] == "solo"
    assert c.get("/branding/favicon.png").content == b"fav"


def test_the_stock_init_marks_are_not_a_custom_logo(tmp_path, monkeypatch):
    """`hubzoid init` copies the Hubzoid wordmark into branding/. The app draws
    that mark itself (readable in dark mode), so an unchanged copy is no logo
    and no avatar; the browser tab still uses the icon."""
    import shutil

    hub = _hub(tmp_path, "solo", 3611)
    shutil.copytree(Path(webapp_gateway.__file__).parent / "templates" / "minimal" / "branding",
                    hub / "branding")
    eng = create_engine(f"sqlite:///{tmp_path / 'op.db'}")
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    from hubzoid.access import store_for

    store_for(hub).grant("admin@localhost", "solo", "use_hub", actor="t")
    app = FastAPI()
    webapp_gateway.mount(app, hub, model_label="solo-label")
    c = TestClient(app)
    assert c.get("/api/agents").json()["agents"][0]["avatar_url"] is None
    branding = c.get("/api/branding").json()
    assert branding["logo_url"] is None and branding["favicon_url"] == "/branding/favicon.svg"
    (hub / "branding" / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    assert c.get("/api/branding").json()["logo_url"] == "/branding/logo.svg"


# ---------------------------------------------------------------------------
# the edge, by mode
# ---------------------------------------------------------------------------
def _edge(monkeypatch, *, routes=(), default="http://bridge1", fallbacks=(), answers=None):
    real_client = httpx.AsyncClient
    seen = []

    def upstream(request):
        seen.append(request)
        answer = (answers or {}).get(request.url.host)
        if isinstance(answer, Exception):
            raise answer
        if request.url.path == "/page":
            return httpx.Response(200, stream=httpx.ByteStream(b"<html><body>app</body></html>"),
                                  headers={"content-type": "text/html"})
        if request.url.path == "/api/models":
            return httpx.Response(200, stream=httpx.ByteStream(json.dumps(
                {"data": [{"id": "sales"}, {"id": "support"}]}).encode()),
                headers={"content-type": "application/json"})
        if request.url.path.startswith("/portal/api/chat-access"):
            return httpx.Response(200, stream=httpx.ByteStream(b'{"denied": ["support"]}'))
        if "callback" in request.url.path:
            return httpx.Response(302, headers={"location": "/"}, stream=httpx.ByteStream(b""))
        return httpx.Response(200, stream=httpx.ByteStream(
            f"{request.url.host}:{request.url.path}".encode()),
            headers=[("set-cookie", "a=1; Path=/"), ("set-cookie", "b=2; Path=/")])

    monkeypatch.setattr(edge.httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(upstream)))
    app = edge.build_edge_app(default_base=default, routes=list(routes), public_scheme="https", default_fallbacks=fallbacks)
    return app, seen

@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/models", "/uploads/c1/x.txt",
                                  "/otel/v1/traces"])
def test_web_app_edge_keeps_the_bridge_api_on_loopback(monkeypatch, path):
    app, seen = _edge(monkeypatch)
    with StarletteClient(app) as c:
        assert c.post(path, content=b"{}").status_code == 404
    assert seen == []
    # Repeated slashes don't sneak past (a client can send them as they are).
    assert edge._is_bridge_internal("//v1//chat/completions")
    assert not edge._is_bridge_internal("/v1x") and not edge._is_bridge_internal("/api/v1/models")


def test_web_app_edge_routes_strip_prefix_and_say_so(monkeypatch):
    routes = [edge.EdgeRoute("/b/sales/api", "http://sales", "/b/sales"),
              edge.EdgeRoute("/b/sales/branding", "http://sales", "/b/sales")]
    app, seen = _edge(monkeypatch, routes=routes)
    with StarletteClient(app) as c:
        r = c.get("/b/sales/api/agents", headers={
            "X-Forwarded-Prefix": "/b/evil", "X-Hubzoid-User": "boss@example.org",
            "X-Hubzoid-Assertion": "v1.forged.sig", "X-OpenWebUI-User-Email": "boss@example.org"})
        assert r.text == "sales:/api/agents"
        assert r.headers.get_list("set-cookie") == ["a=1; Path=/", "b=2; Path=/"]
        c.get("/api/agents", headers={"X-Forwarded-Prefix": "/b/sales"})
    hub_scoped, deployment_wide = seen
    assert hub_scoped.headers["x-forwarded-prefix"] == "/b/sales"
    assert hub_scoped.headers["x-forwarded-proto"] == "https"
    assert not any(k.startswith(("x-hubzoid-", "x-openwebui-")) for k in hub_scoped.headers)
    assert "x-forwarded-prefix" not in deployment_wide.headers


def test_web_app_edge_falls_back_to_the_next_bridge(monkeypatch):
    app, seen = _edge(monkeypatch, fallbacks=("http://bridge2",),
                      answers={"bridge1": httpx.ConnectError("refused")})
    with StarletteClient(app) as c:
        r = c.post("/api/auth/login", json={"email": "a@example.org"})
    assert r.text == "bridge2:/api/auth/login"
    assert [req.url.host for req in seen] == ["bridge1", "bridge2"]
    assert seen[-1].content == b'{"email":"a@example.org"}'

def test_api_base_for_each_hub(deployment_hubs, tmp_path):
    sales, support, _gw, _gs = deployment_hubs
    assert webapp_gateway.api_base_for(sales) == "/b/sales"
    assert webapp_gateway.api_base_for(support) == "/b/support"
    solo = _hub(tmp_path / "solo-root", "solo", 3611)
    assert webapp_gateway.api_base_for(solo) == ""


def test_edge_redacts_oauth_callback_logs_when_connections_provide_it(monkeypatch):
    from hubzoid.connectors import routes as connector_routes

    monkeypatch.setenv("HUBZOID_EDGE_DEFAULT", "http://a")
    monkeypatch.setattr(edge, "build_edge_app", lambda **kw: "app")
    calls = []
    monkeypatch.setattr(connector_routes, "redact_oauth_callback_logs",
                        lambda: calls.append("redacted"), raising=False)
    assert edge._factory() == "app" and calls == ["redacted"]

    def boom():
        raise RuntimeError("no logging config")

    monkeypatch.setattr(connector_routes, "redact_oauth_callback_logs", boom, raising=False)
    assert edge._factory() == "app"            # never stops the front door
    monkeypatch.delattr(connector_routes, "redact_oauth_callback_logs")
    assert edge._factory() == "app"            # a build without connections



def test_an_agents_display_name_is_shown_as_written(tmp_path):
    from hubzoid.loaders import agents as agents_loader
    from hubzoid.webapp_gateway import agent_card

    hub = _hub(tmp_path, "irs", 3613, agent_name="irs-hub")
    assert agent_card(hub)["name"] == "irs-hub"  # the app turns a slug into "Irs Hub"
    (hub / "AGENTS.md").write_text("---\nname: irs-hub\ndisplay_name: IRS Hub\ndescription: d\n---\nbody")
    card = agent_card(hub)
    assert (card["name"], card["id"]) == ("IRS Hub", "irs-hub")  # the id stays the slug
    assert agents_loader.display_name(hub) == "IRS Hub"
    (hub / "AGENTS.md").write_text("---\nname: [broken\n---")
    assert agents_loader.display_name(hub, "IRS") == "IRS"
