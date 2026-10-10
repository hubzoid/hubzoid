"""The edge's account-page controls and the connection-journey callback hook.

`HUBZOID_HIDE_OWUI_USERS=true` opens Settings > Integrations for Open WebUI's
whole Users section (the user list and Groups: people and access are managed in
the Console) and refuses browser writes to Open WebUI's account-admin API;
Hubzoid's own service calls use the internal URL and never pass the edge.
The P2 hook rewrites an OAuth client callback redirect to the journey's done
page when the `hz_connect` cookie is present, and clears the cookie.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from hubzoid.edge import EdgeRoute, build_edge_app


@pytest.fixture(autouse=True)
def _legacy_ui(monkeypatch):
    """These tests pin Open WebUI mode (HUBZOID_UI=openwebui); the web app mode
    is covered by tests/test_gateway_app*.py."""
    monkeypatch.setenv("HUBZOID_UI", "openwebui")



class _Body(httpx.AsyncByteStream):
    """An unread upstream body, as a real server gives the edge's streaming
    pass-through (a Response built from bytes counts as already consumed)."""

    def __init__(self, data: bytes = b""):
        self.data = data

    async def __aiter__(self):
        if self.data:
            yield self.data


class Upstream:
    def __init__(self):
        self.seen: list[tuple[str, str]] = []
        self.callback_status = 307
        self.callback_location = "/"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.path))
        if request.url.path.startswith("/oauth/clients/") or request.url.path.startswith("/oauth/google"):
            if self.callback_status == 200:
                return httpx.Response(200, stream=_Body(b"ok"))
            return httpx.Response(
                self.callback_status,
                headers=[("location", self.callback_location),
                         ("set-cookie", "oauth_session_id=s1; Path=/; HttpOnly"),
                         ("set-cookie", "token=t1; Path=/")],
                stream=_Body(),
            )
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=_Body(b'{"ok": true}'))


@pytest.fixture
def edge(monkeypatch):
    def make(hide: bool):
        if hide:
            monkeypatch.setenv("HUBZOID_HIDE_OWUI_USERS", "true")
        else:
            monkeypatch.delenv("HUBZOID_HIDE_OWUI_USERS", raising=False)
        monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
        upstream = Upstream()
        app = build_edge_app(default_base="http://owui",
                             routes=[EdgeRoute("/portal", "http://bridge")])
        client = TestClient(app, follow_redirects=False)
        client.__enter__()
        original = app.state.client
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream),
                                             follow_redirects=False)

        def close():
            client.portal.call(app.state.client.aclose)
            app.state.client = original
            client.__exit__(None, None, None)

        return client, upstream, close

    made = []

    def factory(hide=False):
        out = make(hide)
        made.append(out[2])
        return out[0], out[1]

    yield factory
    for close in made:
        close()


# ---- Users page ---------------------------------------------------------------------

def test_other_admin_pages_stay(edge):
    paths = ["/admin/evaluations", "/admin/evaluations/leaderboard", "/admin/settings",
             "/admin/settings/integrations", "/admin/functions", "/admin/analytics",
             "/admin/usersx", "/"]
    client, upstream = edge(hide=True)
    for path in paths:
        assert client.get(path).status_code == 200, path
    assert upstream.seen == [("GET", path) for path in paths]


def test_the_admin_panel_is_untouched_when_not_hidden(edge):
    client, upstream = edge(hide=False)
    for path in ("/admin", "/admin/users/overview"):
        assert client.get(path).status_code == 200
    assert upstream.seen == [("GET", "/admin"), ("GET", "/admin/users/overview")]


def test_nothing_changes_when_not_hidden(edge):
    client, upstream = edge(hide=False)
    assert client.get("/admin/users").status_code == 200
    assert client.post("/api/v1/auths/add", json={}).status_code == 200
    assert client.delete("/api/v1/users/u1").status_code == 200
    assert ("POST", "/api/v1/auths/add") in upstream.seen


# ---- the Users section opens Settings ------------------------------------------------

def test_the_whole_users_section_opens_settings(edge):
    """Access is managed in the Console, so Open WebUI's groups decide nothing:
    neither the user list nor Groups shows. The Admin Panel and any typed
    /admin/users address open Settings > Integrations (Open WebUI 0.11's
    `?settings=` dialog over Evaluations, a page that stays)."""
    from hubzoid.edge import SETTINGS_LANDING

    client, upstream = edge(hide=True)
    for path in ("/admin", "/admin/", "//admin", "/admin/users", "/admin/users/",
                 "/admin/users/overview", "/admin/users/groups", "/admin/users/groups/",
                 "//admin/users/groups", "/admin/users/anything"):
        url = "http://testserver" + path
        r = client.get(url)
        assert (r.status_code, r.headers.get("location")) == (302, SETTINGS_LANDING), path
        assert client.head(url).status_code == 302, path
    assert upstream.seen == []
    assert SETTINGS_LANDING.endswith("?settings=admin%3Aintegrations")
    assert client.get(SETTINGS_LANDING).status_code == 200  # the landing never redirects again


def test_hiding_groups_is_not_access_control(edge):
    """Hidden links are not access control. Account-admin writes stay refused;
    group writes keep today's behaviour (HUBZOID_LOCK_OWUI_ACCESS_UI locks them)."""
    client, upstream = edge(hide=True)
    for method, path, status in [
        ("POST", "/api/v1/auths/add", 403),                    # accounts: still refused
        ("DELETE", "/api/v1/users/0b5c-uuid", 403),
        ("POST", "/api/v1/groups/create", 200),                # groups: unchanged (not locked)
        ("POST", "/api/v1/groups/id/g1/update", 200),
        ("GET", "/api/v1/groups/", 200),
    ]:
        upstream.seen.clear()
        assert client.request(method, path, json={}).status_code == status, path
        assert (upstream.seen == [(method, path)]) is (status == 200), path


def test_account_admin_writes_are_blocked(edge):
    client, upstream = edge(hide=True)
    for method, path in [
        ("POST", "/api/v1/auths/add"),
        ("POST", "/api/v1/auths/add/"),
        ("POST", "/api/v1/users/0b5c-uuid/update"),
        ("DELETE", "/api/v1/users/0b5c-uuid"),
        ("POST", "//api/v1/users/0b5c-uuid/update"),
    ]:
        r = client.request(method, "http://testserver" + path, json={"role": "admin"})
        assert r.status_code == 403 and "Console" in r.text, path
    assert upstream.seen == []


def test_own_settings_and_reads_pass_through(edge):
    client, upstream = edge(hide=True)
    calls = [
        ("POST", "/api/v1/users/user/settings/update"),
        ("POST", "/api/v1/users/user/info/update"),
        ("POST", "/api/v1/users/user/status/update"),
        ("GET", "/api/v1/users/0b5c-uuid"),
        ("GET", "/api/v1/users/"),
        ("POST", "/api/v1/auths/signin"),
        ("POST", "/api/v1/groups/create"),
    ]
    for method, path in calls:
        assert client.request(method, path, json={}).status_code == 200, path
    assert upstream.seen == calls


def test_navigation_script_carries_the_flag(edge):
    from hubzoid.edge import SETTINGS_LANDING

    client, _ = edge(hide=True)
    r = client.get("/hubzoid-portal-navigation.js")
    body = r.text
    assert "const HIDE_USERS = true;" in body
    # In-app links to the Admin Panel land where the edge sends a full page load.
    assert f"const SETTINGS = '{SETTINGS_LANDING}';" in body
    assert r.headers["cache-control"] == "no-cache"
    assert "['/admin', '/admin/users', '/admin/users/overview']" in body
    assert "/portal/api/me?brief=1" in body
    # An empty chat is explained: blocked, or no agent yet.
    assert "/portal/api/chat-access" in body and "blocked by an administrator" in body
    # ...right after an in-app sign-in, not at the next 15-second check.
    assert "location.pathname !== lastPath" in body
    client2, _ = edge(hide=False)
    assert "const HIDE_USERS = false;" in client2.get("/hubzoid-portal-navigation.js").text


# ---- Open WebUI tool servers: MCP connectors are managed in the Console ----------------

@pytest.mark.parametrize("path", ["/api/v1/configs/tool_servers", "/api/v1/configs/tool_servers/verify",
                                  "/api/v1/configs/oauth/clients/register"])
def test_open_webui_tool_server_saves_are_refused(edge, path):
    client, _ = edge(hide=False)
    r = client.post(path, json={})
    assert r.status_code == 403 and "Console" in r.json()["detail"]


def test_open_webui_oauth_routes_pass_through(edge):
    """Open WebUI's own sign-in and client callbacks are not touched."""
    client, _ = edge()
    assert client.get("/oauth/clients/mcp:gmail/callback").headers["location"] == "/"
    assert client.get("/oauth/google/callback").headers["location"] == "/"


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("HUBZOID_OPERATIONAL_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)


# ---- the deployment's default (release review) ---------------------------------------

def _manifest(tmp_path, **extra):
    import json

    path = tmp_path / "deployment.json"
    path.write_text(json.dumps({"version": 1, "hubs": [], **extra}))
    return str(path)


@pytest.mark.parametrize("recorded,env,hidden", [
    (True, None, True),        # a gateway set up fresh with Console accounts
    (None, None, False),       # an existing deployment records nothing: unchanged
    (True, "false", False),    # an explicit setting wins either way
    (None, "true", True),
])
def test_hiding_follows_the_recorded_default_unless_set(tmp_path, monkeypatch, recorded, env, hidden):
    from hubzoid.edge import _hide_owui_users

    extra = {} if recorded is None else {"hide_owui_users": recorded}
    envmap = {"HUBZOID_DEPLOYMENT": _manifest(tmp_path, **extra)}
    if env is not None:
        envmap["HUBZOID_HIDE_OWUI_USERS"] = env
    assert _hide_owui_users(envmap) is hidden


@pytest.mark.parametrize("prior,fresh,env,expected", [
    ({}, True, {"WEBUI_AUTH": "true", "HUBZOID_GATEWAY_ADMIN_EMAIL": "o@x.org",
                "HUBZOID_GATEWAY_ADMIN_PASSWORD": "pw"}, True),     # new, Console accounts
    ({}, True, {"WEBUI_AUTH": "true"}, None),                      # no service account
    ({}, True, {"HUBZOID_GATEWAY_ADMIN_EMAIL": "o@x.org",
                "HUBZOID_GATEWAY_ADMIN_PASSWORD": "pw"}, None),     # sign-in off
    ({}, False, {"WEBUI_AUTH": "true", "HUBZOID_GATEWAY_ADMIN_EMAIL": "o@x.org",
                 "HUBZOID_GATEWAY_ADMIN_PASSWORD": "pw"}, None),    # existing chat data (upgrade)
    ({"version": 1}, True, {"WEBUI_AUTH": "true", "HUBZOID_GATEWAY_ADMIN_EMAIL": "o@x.org",
                            "HUBZOID_GATEWAY_ADMIN_PASSWORD": "pw"}, None),  # earlier manifest
    ({"hide_owui_users": True}, False, {}, True),                  # kept once recorded
    ({"hide_owui_users": False}, True, {"WEBUI_AUTH": "true"}, False),
])
def test_the_gateway_records_hiding_only_for_new_console_deployments(prior, fresh, env, expected):
    from hubzoid.deployment import hide_owui_users_default

    assert hide_owui_users_default(prior, fresh=fresh, env=env) == expected
