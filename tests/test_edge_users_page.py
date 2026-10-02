"""The edge's account-page controls and the connection-journey callback hook.

`HUBZOID_HIDE_OWUI_USERS=true` sends browser navigation to Open WebUI's Users
page to the Console and refuses browser writes to Open WebUI's account-admin
API; Hubzoid's own service calls use the internal URL and never pass the edge.
When every hub is also managed in the Console, Groups is hidden too and the
whole Users section opens Settings > Integrations.
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
    """These tests pin the legacy Open WebUI mode (HUBZOID_UI=openwebui); the
    web app mode is covered by tests/test_gateway_app*.py."""
    monkeypatch.setenv("HUBZOID_UI", "openwebui")

JOURNEY = "abcDEF0123456789_-xyzQ"  # 22 chars, matches the contract pattern


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
    def make(hide: bool, managed: bool):
        if hide:
            monkeypatch.setenv("HUBZOID_HIDE_OWUI_USERS", "true")
        else:
            monkeypatch.delenv("HUBZOID_HIDE_OWUI_USERS", raising=False)
        monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
        # Whether every hub is Console-managed (read from the deployment; see
        # the tests of `_fully_managed` below).
        monkeypatch.setattr("hubzoid.edge._fully_managed", lambda env: managed)
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

    def factory(hide=False, managed=False):
        out = make(hide, managed)
        made.append(out[2])
        return out[0], out[1]

    yield factory
    for close in made:
        close()


# ---- Users page ---------------------------------------------------------------------

@pytest.mark.parametrize("path,location", [
    ("/admin/users/overview", "/portal/#/people"),
    # Release review: hiding the user list must keep Groups reachable from the
    # Admin Panel's Users tab.
    ("/admin/users", "/admin/users/groups"),
    # UX review: the Admin Panel opens where an administrator can work:
    # Settings > Integrations (Open WebUI 0.11's admin settings dialog) over Groups.
    ("/admin", "/admin/users/groups?settings=admin%3Aintegrations"),
], ids=["users-page", "users-section", "admin-panel"])
def test_hidden_users_pages_redirect(edge, path, location):
    from hubzoid.edge import ADMIN_LANDING

    assert ADMIN_LANDING == "/admin/users/groups?settings=admin%3Aintegrations"
    client, upstream = edge(hide=True)
    for variant in (path, path + "/", "/" + path):
        url = "http://testserver" + variant  # keep a leading "//" as a path, not a host
        r = client.get(url)
        assert (r.status_code, r.headers.get("location")) == (302, location), variant
        assert client.head(url).status_code == 302, variant
    assert upstream.seen == []


@pytest.mark.parametrize("managed,paths", [
    (False, ["/admin/users/groups", "/admin/settings", "/admin/settings/integrations",
             "/admin/evaluations", "/admin/functions", "/admin/analytics", "/"]),
    (True, ["/admin/evaluations", "/admin/evaluations/leaderboard", "/admin/settings",
            "/admin/settings/integrations", "/admin/functions", "/admin/usersx", "/"]),
], ids=["hidden", "every-hub-managed"])
def test_other_admin_pages_stay(edge, managed, paths):
    client, upstream = edge(hide=True, managed=managed)
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


# ---- every hub managed in the Console: Groups is hidden too ---------------------------

def test_the_whole_users_section_opens_settings_when_every_hub_is_managed(edge):
    """Console simplification: with every agent managed in the Console, Open
    WebUI's groups decide nothing, so neither the user list nor Groups shows. The
    Admin Panel and any typed /admin/users address open Settings > Integrations
    (Open WebUI 0.11's `?settings=` dialog over Evaluations, a page that stays)."""
    from hubzoid.edge import SETTINGS_LANDING

    client, upstream = edge(hide=True, managed=True)
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


def test_managed_hubs_alone_change_nothing_while_the_users_page_shows(edge):
    """Groups is hidden only together with the user list."""
    client, upstream = edge(hide=False, managed=True)
    for path in ("/admin", "/admin/users/groups", "/admin/users/overview"):
        assert client.get(path).status_code == 200
    assert "const HIDE_GROUPS = false;" in client.get("/hubzoid-portal-navigation.js").text


def test_hiding_groups_is_not_access_control(edge):
    """Hidden links are not access control. Account-admin writes stay refused;
    group writes keep today's behaviour (HUBZOID_LOCK_OWUI_ACCESS_UI locks them)."""
    client, upstream = edge(hide=True, managed=True)
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
    from hubzoid.edge import ADMIN_LANDING, GROUPS_URL

    client, _ = edge(hide=True)
    body = client.get("/hubzoid-portal-navigation.js").text
    assert "const HIDE_USERS = true;" in body
    # In-app links to the Admin Panel land where the edge sends a full page load.
    assert f"const ADMIN_LANDING = '{ADMIN_LANDING}';" in body
    assert f"const GROUPS = '{GROUPS_URL}';" in body
    assert "['/admin', '/admin/users', '/admin/users/overview']" in body
    assert "/portal/api/me?brief=1" in body
    # An empty chat is explained: blocked, or no agent yet.
    assert "/portal/api/chat-access" in body and "blocked by an administrator" in body
    # ...right after an in-app sign-in, not at the next 15-second check.
    assert "location.pathname !== lastPath" in body
    client2, _ = edge(hide=False)
    assert "const HIDE_USERS = false;" in client2.get("/hubzoid-portal-navigation.js").text


@pytest.mark.parametrize("managed", [False, True])
def test_navigation_script_hides_groups_only_when_every_hub_is_managed(edge, managed):
    from hubzoid.edge import SETTINGS_LANDING

    client, _ = edge(hide=True, managed=managed)
    r = client.get("/hubzoid-portal-navigation.js")
    assert "const HIDE_USERS = true;" in r.text
    assert f"const HIDE_GROUPS = {'true' if managed else 'false'};" in r.text
    assert f"const SETTINGS = '{SETTINGS_LANDING}';" in r.text
    # Decided per request, so a browser must not keep an earlier answer.
    assert r.headers["cache-control"] == "no-cache"


# ---- connection-journey callback (edge contract for P2) --------------------------------

def _set_cookie_values(r):
    return [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]


@pytest.mark.parametrize("location", ["/", "/?error=access_denied"])
def test_callback_redirect_goes_to_the_journey(edge, location):
    client, upstream = edge()
    upstream.callback_location = location
    client.cookies.set("hz_connect", JOURNEY)
    r = client.get("/oauth/clients/mcp:gmail/callback", params={"code": "c", "state": "s"})
    assert r.status_code == 307
    assert r.headers["location"] == f"/portal/connect/{JOURNEY}/done"
    cookies = _set_cookie_values(r)
    assert "oauth_session_id=s1; Path=/; HttpOnly" in cookies and "token=t1; Path=/" in cookies
    assert "hz_connect=; Max-Age=0; Path=/" in cookies


def test_callback_untouched_without_a_valid_journey(edge):
    client, upstream = edge()
    for cookie in (None, "short", "has spaces in it 1234567", "x" * 65, "bad!chars" * 3):
        client.cookies.clear()
        if cookie:
            client.cookies.set("hz_connect", cookie)
        r = client.get("/oauth/clients/mcp:gmail/callback")
        assert r.status_code == 307 and r.headers["location"] == "/", cookie
        assert not any(v.startswith("hz_connect=") for v in _set_cookie_values(r)), cookie


def test_callback_untouched_for_other_flows(edge):
    client, upstream = edge()
    client.cookies.set("hz_connect", JOURNEY)
    # Sign-in with Google is a different flow.
    r = client.get("/oauth/google/callback")
    assert r.headers["location"] == "/"
    # A non-redirect response and a non-GET are left alone.
    upstream.callback_status = 200
    assert client.get("/oauth/clients/mcp:gmail/callback").status_code == 200
    upstream.callback_status = 307
    r = client.post("/oauth/clients/mcp:gmail/callback")
    assert r.headers["location"] == "/"



# ---- fully managed: every hub authoritative in the access store ----------------------

def _deployment(tmp_path, names=("finance", "ops")):
    from hubzoid import deployment

    dirs = []
    for name in names:
        (tmp_path / name).mkdir()
        dirs.append(tmp_path / name)
    path = tmp_path / "gateway" / "deployment.json"
    deployment.save(path, hubs=[dict(key=p.name, name=p.name, path=str(p), model_id=p.name)
                                for p in dirs],
                    operational_url=f"sqlite:///{tmp_path}/ops.db", owui_url="http://owui",
                    owui_db=str(tmp_path / "owui.db"))
    return str(path), dirs


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("HUBZOID_OPERATIONAL_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)


def test_fully_managed_needs_every_hub_on_console_access(tmp_path, monkeypatch, clean_env):
    from hubzoid.access import store_for
    from hubzoid.edge import _fully_managed

    manifest, dirs = _deployment(tmp_path)
    monkeypatch.setenv("HUBZOID_DEPLOYMENT", manifest)
    env = {"HUBZOID_DEPLOYMENT": manifest}
    gs = store_for(dirs[0])
    assert _fully_managed(env) is False                   # both legacy
    gs.set_authoritative(True, hub="finance")
    assert _fully_managed(env) is False                   # mixed: ops still legacy
    gs.set_authoritative(True, hub="ops")
    assert _fully_managed(env) is True                    # every hub managed
    gs.set_authoritative(False, hub="ops")                # read again on each use
    assert _fully_managed(env) is False


def test_fully_managed_is_off_without_a_readable_deployment(tmp_path, clean_env):
    from hubzoid.edge import _fully_managed

    assert _fully_managed({}) is False                    # standalone `hubzoid run`
    assert _fully_managed({"HUBZOID_DEPLOYMENT": _manifest(tmp_path)}) is False  # no hubs
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert _fully_managed({"HUBZOID_DEPLOYMENT": str(broken)}) is False
    assert _fully_managed({"HUBZOID_DEPLOYMENT": str(tmp_path / "missing.json")}) is False


def test_the_edge_follows_the_hubs_access_mode(tmp_path, monkeypatch, clean_env):
    """End to end through the edge with a real deployment and access store: a
    mixed deployment keeps Groups, a fully managed one opens Settings."""
    from hubzoid.access import store_for
    from hubzoid.edge import ADMIN_LANDING, SETTINGS_LANDING

    manifest, dirs = _deployment(tmp_path)
    monkeypatch.setenv("HUBZOID_DEPLOYMENT", manifest)
    monkeypatch.setenv("HUBZOID_HIDE_OWUI_USERS", "true")
    gs = store_for(dirs[0])
    gs.set_authoritative(True, hub="finance")
    gs.set_authoritative(False, hub="ops")
    app = build_edge_app(default_base="http://127.0.0.1:1",
                         routes=[EdgeRoute("/portal", "http://127.0.0.1:2")])
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/admin").headers["location"] == ADMIN_LANDING
        assert client.get("/admin/users").headers["location"] == "/admin/users/groups"
        assert "const HIDE_GROUPS = false;" in client.get("/hubzoid-portal-navigation.js").text
        gs.set_authoritative(True, hub="ops")
        for path in ("/admin", "/admin/users", "/admin/users/groups", "/admin/users/overview"):
            assert client.get(path).headers["location"] == SETTINGS_LANDING
        assert "const HIDE_GROUPS = true;" in client.get("/hubzoid-portal-navigation.js").text


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
