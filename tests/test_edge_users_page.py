"""The edge's account-page controls and the connection-journey callback hook.

`HUBZOID_HIDE_OWUI_USERS=true` sends browser navigation to Open WebUI's Users
page to the Console and refuses browser writes to Open WebUI's account-admin
API; Hubzoid's own service calls use the internal URL and never pass the edge.
The P2 hook rewrites an OAuth client callback redirect to the journey's done
page when the `hz_connect` cookie is present, and clears the cookie.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from hubzoid.edge import EdgeRoute, build_edge_app

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
    def make(hide: bool):
        if hide:
            monkeypatch.setenv("HUBZOID_HIDE_OWUI_USERS", "true")
        else:
            monkeypatch.delenv("HUBZOID_HIDE_OWUI_USERS", raising=False)
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

@pytest.mark.parametrize("path", ["/admin/users", "/admin/users/", "/admin/users/overview",
                                  "/admin/users/overview/", "//admin/users"])
def test_users_page_redirects_to_people_when_hidden(edge, path):
    client, upstream = edge(hide=True)
    path = "http://testserver" + path  # keep a leading "//" as a path, not a host
    r = client.get(path)
    assert r.status_code == 302 and r.headers["location"] == "/portal/#/people"
    assert client.head(path).status_code == 302
    assert upstream.seen == []


@pytest.mark.parametrize("path", ["/admin/users/groups", "/admin/settings", "/admin", "/"])
def test_other_admin_pages_stay(edge, path):
    client, upstream = edge(hide=True)
    assert client.get(path).status_code == 200
    assert upstream.seen == [("GET", path)]


def test_nothing_changes_when_not_hidden(edge):
    client, upstream = edge(hide=False)
    assert client.get("/admin/users").status_code == 200
    assert client.post("/api/v1/auths/add", json={}).status_code == 200
    assert client.delete("/api/v1/users/u1").status_code == 200
    assert ("POST", "/api/v1/auths/add") in upstream.seen


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/auths/add"),
    ("POST", "/api/v1/auths/add/"),
    ("POST", "/api/v1/users/0b5c-uuid/update"),
    ("DELETE", "/api/v1/users/0b5c-uuid"),
    ("POST", "//api/v1/users/0b5c-uuid/update"),
])
def test_account_admin_writes_are_blocked(edge, method, path):
    client, upstream = edge(hide=True)
    r = client.request(method, "http://testserver" + path, json={"role": "admin"})
    assert r.status_code == 403 and "Console" in r.text
    assert upstream.seen == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/v1/users/user/settings/update"),
    ("POST", "/api/v1/users/user/info/update"),
    ("POST", "/api/v1/users/user/status/update"),
    ("GET", "/api/v1/users/0b5c-uuid"),
    ("GET", "/api/v1/users/"),
    ("POST", "/api/v1/auths/signin"),
    ("POST", "/api/v1/groups/create"),
])
def test_own_settings_and_reads_pass_through(edge, method, path):
    client, upstream = edge(hide=True)
    assert client.request(method, path, json={}).status_code == 200
    assert upstream.seen == [(method, path)]


def test_service_calls_do_not_use_the_edge(monkeypatch):
    """The Console's account adapter talks to Open WebUI's internal URL, so the
    edge block cannot affect it even with the page hidden."""
    from hubzoid.access.accounts import OwuiAccounts

    from tests.test_access_service import SERVICE, FakeOwui

    monkeypatch.setenv("HUBZOID_HIDE_OWUI_USERS", "true")
    fake = FakeOwui()
    created = OwuiAccounts("http://127.0.0.1:43080", SERVICE, "svc-secret",
                           transport=httpx.MockTransport(fake)).create(
        email="ann@x.org", name="Ann", password="Correct-Horse-7")
    assert created["role"] == "user"


def test_navigation_script_carries_the_flag(edge):
    client, _ = edge(hide=True)
    body = client.get("/hubzoid-portal-navigation.js").text
    assert "const HIDE_USERS = true;" in body and "/portal/#/people" in body
    assert "/portal/api/me?brief=1" in body
    client2, _ = edge(hide=False)
    assert "const HIDE_USERS = false;" in client2.get("/hubzoid-portal-navigation.js").text


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


@pytest.mark.parametrize("cookie", [None, "short", "has spaces in it 1234567", "x" * 65, "bad!chars" * 3])
def test_callback_untouched_without_a_valid_journey(edge, cookie):
    client, upstream = edge()
    if cookie:
        client.cookies.set("hz_connect", cookie)
    r = client.get("/oauth/clients/mcp:gmail/callback")
    assert r.status_code == 307 and r.headers["location"] == "/"
    assert not any(v.startswith("hz_connect=") for v in _set_cookie_values(r))


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
