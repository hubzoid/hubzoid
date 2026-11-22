"""Provider HTTP setup, OAuth returns and administrative capability forwarding."""
from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from hubzoid import hub_routes
from hubzoid.channels import config, router as channel_router, store
from hubzoid.channels.errors import ChannelError
from hubzoid.control import authz


@pytest.fixture
def api(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "WEBUI_AUTH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.test/b/desk")
    root = tmp_path / "desk"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nDesk.")
    conn = SimpleNamespace(id="main", hub="desk", hub_dir=root, provider="email", revision=7)
    monkeypatch.setattr(config, "active", lambda *a: conn)
    permissions = []
    async def require(request, path, cap, hub, *, mutation=False):
        permissions.append((cap, mutation))
        return SimpleNamespace(email="owner@example.org", org_admin=False)
    monkeypatch.setattr(authz, "require_request", require)
    monkeypatch.setattr(channel_router, "kick", lambda *a: None)
    from hubzoid import editing
    monkeypatch.setattr(editing, "check", lambda *a: None)
    app = FastAPI()
    route = hub_routes.router(root)
    for name in ("email", "linear", "github", "slack"):
        import_module(f"hubzoid.channels.{name}.routes").mount(route, root)
    app.include_router(route)
    with TestClient(app) as client:
        yield client, conn, permissions


BASE = "/api/hubs/desk/channels/main"


@pytest.mark.parametrize("provider,path,method,module,function,result,kwargs", [
    ("email", "/email/status", "get", "status", "status", {"connected": True}, {}),
    ("email", "/email/connect", "post", "oauth", "connect", {"authorization_url": "https://consent.test"},
     {"json": {"mode": "draft"}}),
    ("email", "/email/poll", "post", "poller", "poll_once", {"state": "started"}, {}),
    ("email", "/email/rehearse-draft", "post", "status", "rehearse_draft", {"draft_verified": True}, {}),
    ("linear", "/linear/setup", "get", "oauth", "setup_checklist", {"installed": False}, {}),
    ("linear", "/linear/connect", "post", "oauth", "connect", {"authorization_url": "https://consent.test"}, {}),
    ("linear", "/linear/disconnect", "post", "oauth", "disconnect", {"disconnected": True}, {}),
    ("github", "/github/setup", "get", "setup", "status", {"state": "none"}, {}),
    ("github", "/github/setup", "post", "setup", "start", {"registration_url": "https://register.test"},
     {"json": {"owner": "example", "type": "organization", "name": "App"}}),
    ("github", "/github/setup/verify", "post", "setup", "installed", {"installation_id": 9},
     {"json": {"installation_id": 9}}),
    ("github", "/github/import-app", "post", "setup", "import_app", {"revision": 8},
     {"json": {"app_id": 1, "installation_id": 9, "owner_id": 3}}),
    ("github", "/github/configure", "post", "setup", "configure", {"revision": 8},
     {"json": {"expected_revision": 7, "repositories": [], "set": {"enabled": True}}}),
    ("github", "/github/intake", "post", "setup", "manual_intake", {"delivery_id": "d1"},
     {"json": {"url": "https://github.com/example/x/issues/1", "task": "Fix"}, "headers": {"idempotency-key": "once"}}),
    ("github", "/github/operations", "post", "operations", "call", {"data": {"id": 1}},
     {"json": {"name": "getRepository", "arguments": {"repository_id": 1}}}),
])
def test_provider_admin_routes(api, monkeypatch, provider, path, method, module, function, result, kwargs):
    client, conn, permissions = api
    conn.provider = provider
    fn = Mock(return_value=result)
    monkeypatch.setattr(import_module(f"hubzoid.channels.{provider}.{module}"), function, fn)
    r = getattr(client, method)(BASE + path, **kwargs)
    assert r.status_code in (200, 201, 202) and r.json() == result
    assert fn.call_args.args[0] is conn
    assert permissions == [("channels.inspect" if method == "get" else "channels.manage", method != "get")]
    if function == "configure":
        assert fn.call_args.kwargs["any_secret"] is False
        assert fn.call_args.kwargs["expected_revision"] == 7
    if function == "manual_intake":
        assert fn.call_args.kwargs["key"] == "once"
    if provider == "email" and function == "connect":
        assert fn.call_args.kwargs == {"actor": "owner@example.org", "mode": "draft"}


@pytest.mark.parametrize("provider,path", [("email", "/email/status"), ("linear", "/linear/setup"),
                                           ("github", "/github/setup")])
def test_provider_routes_refuse_wrong_provider_or_missing_connection(api, monkeypatch, provider, path):
    client, conn, _ = api
    conn.provider = "fake"
    r = client.get(BASE + path)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"
    def missing(*a):
        raise ChannelError("not_found", "Missing connection", status=404)
    monkeypatch.setattr(config, "active", missing)
    r = client.get(BASE + path)
    assert r.status_code == 404 and r.json()["detail"]["message"] == "Missing connection"


@pytest.mark.parametrize("provider,module,function,path", [("email", "oauth", "connect", "/email/connect"),
    ("linear", "oauth", "connect", "/linear/connect"), ("github", "setup", "start", "/github/setup")])
def test_provider_service_errors_keep_status_and_message(api, monkeypatch, provider, module, function, path):
    client, conn, _ = api
    conn.provider = provider
    fn = Mock(side_effect=ChannelError("auth_required", "Reconnect first", status=409))
    monkeypatch.setattr(import_module(f"hubzoid.channels.{provider}.{module}"), function, fn)
    r = client.post(BASE + path, content="")
    assert r.status_code == 409 and r.json()["detail"] == {"code": "auth_required", "message": "Reconnect first"}


@pytest.mark.parametrize("provider,result,word", [("email", {"mailbox": "desk@example.org"}, "desk@example.org"),
                                                ("linear", {"organization": "Example"}, "Example")])
def test_oauth_pages_escape_errors_and_show_success(api, monkeypatch, provider, result, word):
    client, conn, permissions = api
    conn.provider = provider
    oauth = import_module(f"hubzoid.channels.{provider}.oauth")
    fn = Mock(return_value=result)
    monkeypatch.setattr(oauth, "callback", fn)
    r = client.get(BASE + f"/{provider}/oauth/callback", params={"code": "code", "state": "state"})
    assert r.status_code == 200 and word in r.text
    assert fn.call_args.kwargs == {"code": "code", "state": "state"}
    r = client.get(BASE + f"/{provider}/oauth/callback", params={"error": "<script>oops</script>"})
    assert r.status_code == 400 and "&lt;script&gt;" in r.text and "<script>oops" not in r.text
    fn.side_effect = ChannelError("expired", "State expired", status=410)
    r = client.get(BASE + f"/{provider}/oauth/callback")
    assert r.status_code == 410 and "State expired" in r.text
    assert permissions == []


def test_email_connect_invalid_json_defaults_to_approval(api, monkeypatch):
    client, _, _ = api
    from hubzoid.channels.email import oauth
    fn = Mock(return_value={"mode": "approve"})
    monkeypatch.setattr(oauth, "connect", fn)
    r = client.post(BASE + "/email/connect", content="{")
    assert r.status_code == 201 and r.json() == {"mode": "approve"}
    assert fn.call_args.kwargs["mode"] == "approve"


def test_linear_sessions_serialization(api, monkeypatch):
    client, conn, _ = api
    conn.provider = "linear"
    monkeypatch.setattr(store, "list_threads", lambda *a, **kw: {"items": [{"id": "t1", "requester": "ana",
        "updated": 10, "origin_json": {"coordinates": {"agent_session_id": "s1"}, "url": "https://linear.test/s1"}}],
        "next_cursor": "next"})
    r = client.get(BASE + "/linear/sessions?limit=2&cursor=old")
    assert r.status_code == 200 and r.json() == {"items": [{"thread_id": "t1", "requester": "ana", "updated": 10,
        "agent_session_id": "s1", "url": "https://linear.test/s1"}], "next_cursor": "next"}


def test_github_setup_returns_and_repository_wrappers(api, monkeypatch):
    from hubzoid.channels.github import setup
    client, conn, _ = api
    conn.provider = "github"
    monkeypatch.setattr(setup, "registration_page", lambda *a: "<h1>Register app</h1>")
    monkeypatch.setattr(setup, "callback", lambda *a: "https://github.com/apps/test/installations/new")
    monkeypatch.setattr(setup, "installed", lambda *a, **kw: {"installation_id": 9})
    monkeypatch.setattr(setup, "repositories", lambda *a: [{"id": 1}])
    monkeypatch.setattr(setup, "resolve_names", lambda *a: [{"id": 1, "full_name": "example/x"}])
    cancel = Mock()
    monkeypatch.setattr(setup, "cancel", cancel)
    assert client.get(BASE + "/github/register").text == "<h1>Register app</h1>"
    r = client.get(BASE + "/github/callback", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://github.com/apps/test/installations/new"
    r = client.get(BASE + "/github/installed?installation_id=9&state=s", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://hub.example.test/settings#/agents/desk/channels/main?connected=github"
    r = client.get(BASE + "/github/repositories")
    assert r.status_code == 200 and r.json() == {"items": [{"id": 1}]}
    r = client.post(BASE + "/github/repositories/resolve", json={"names": ["example/x"]})
    assert r.json() == {"repositories": [{"id": 1, "full_name": "example/x"}], "revision": 7}
    assert client.delete(BASE + "/github/setup").status_code == 204
    assert cancel.call_args.args == (conn, "owner@example.org")


@pytest.mark.parametrize("path,function", [("register", "registration_page"), ("callback", "callback"),
                                           ("installed", "installed")])
def test_github_public_returns_explain_failure(api, monkeypatch, path, function):
    from hubzoid.channels.github import setup
    client, _, permissions = api
    monkeypatch.setattr(setup, function, Mock(side_effect=ChannelError("expired", "<bad state>", status=410)))
    r = client.get(BASE + f"/github/{path}")
    assert r.status_code == 410 and "&lt;bad state&gt;" in r.text
    assert permissions == []


@pytest.mark.parametrize("raw,status", [("{", 400), ("[]", 200)])
def test_github_body_parser(api, monkeypatch, raw, status):
    from hubzoid.channels.github import setup
    client, conn, _ = api
    conn.provider = "github"
    fn = Mock(return_value={"revision": 8})
    monkeypatch.setattr(setup, "configure", fn)
    r = client.post(BASE + "/github/configure", content=raw)
    assert r.status_code == status
    if status == 200:
        assert fn.call_args.kwargs["sets"] == {} and fn.call_args.kwargs["expected_revision"] == 0
    else:
        assert r.json()["detail"]["message"] == "Send JSON."
