"""First-use identity and model visibility boundaries; no live accounts."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from hubzoid import deployment, portal, webui
from hubzoid.edge import build_edge_app, EdgeRoute


def test_task_header_is_only_added_to_owned_loopback_connections():
    config = {'1': {'headers': {'Existing': 'kept'}}}
    result = webui._local_task_headers(config, {'OPENAI_API_BASE_URLS': 'http://127.0.0.1:8000/v1;https://provider.example/v1'})
    assert result['0']['headers'] == {'X-Hubzoid-Task': '{{TASK}}'}
    assert result['1'] == {'headers': {'Existing': 'kept'}}


def test_deployment_label_matches_bridge_without_loading_environment(tmp_path, monkeypatch):
    (tmp_path / 'AGENTS.md').write_text('---\nname: My Helpful Agent\n---\nHelp the team.\n')
    (tmp_path / '.env').write_text('MODEL_LABEL=example-agent\nDO_NOT_IMPORT=yes\n')
    monkeypatch.delenv('DO_NOT_IMPORT', raising=False)
    import os
    assert deployment.hubs(tmp_path)[0]['model_id'] == 'example-agent'
    assert 'DO_NOT_IMPORT' not in os.environ


@pytest.mark.parametrize('status,expected', [(401, 401), (503, 503)])
def test_account_failure_preserves_signin_vs_service_error(tmp_path, monkeypatch, status, expected):
    from fastapi import HTTPException
    monkeypatch.delenv('HUBZOID_PORTAL_DEV', raising=False)
    monkeypatch.setattr(deployment, 'owui_url', lambda _: 'http://127.0.0.1:43080')
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: httpx.Response(status))
    req = Request({'type': 'http', 'headers': [(b'cookie', b'token=verified')],
                   'method': 'GET', 'path': '/'})
    with pytest.raises(HTTPException) as exc:
        portal.default_admin_resolver(tmp_path)(req)
    assert exc.value.status_code == expected


@pytest.mark.parametrize('role,email,expected', [('admin','admin@localhost',True), ('user','admin@localhost',False), ('admin','other@example.com',False)])
def test_only_verified_configured_owner_is_provisioned(tmp_path, monkeypatch, role, email, expected):
    from unittest.mock import Mock
    store = Mock()
    from hubzoid.access import session
    monkeypatch.setattr(session, 'store_for', lambda _: store)
    monkeypatch.setattr(deployment, 'owui_url', lambda _: 'http://127.0.0.1:43080')
    monkeypatch.delenv('WEBUI_AUTH', raising=False)
    monkeypatch.delenv('HUBZOID_DEPLOYMENT', raising=False)
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: httpx.Response(200, json={'role':role,'email':email,'id':'u'}))
    req = Request({'type':'http','headers':[(b'cookie',b'token=verified')], 'method':'GET','path':'/'})
    assert portal._verify_owui_session(req, tmp_path) == email
    assert store.provision_owner.called is expected


@pytest.mark.parametrize('access_status,expected_status', [(200,200),(401,401),(500,503)])
def test_edge_picker_filters_admins_and_fails_closed(access_status, expected_status):
    app = build_edge_app(default_base='http://owui', routes=[EdgeRoute('/portal','http://bridge')])
    async def handle(req):
        if req.url.path == '/api/models':
            return httpx.Response(200, json={'data':[{'id':'allowed'},{'id':'denied'}]})
        assert req.url.path == '/portal/api/chat-access'
        assert req.headers['cookie'] == 'token=verified'
        assert 'x-hubzoid-user' not in req.headers
        return httpx.Response(access_status, json={'denied':['denied']})
    with TestClient(app) as client:
        original = app.state.client
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            r = client.get('/api/models', headers={'cookie':'token=verified','x-hubzoid-user':'forged'})
            assert r.status_code == expected_status
            if r.status_code == 200:
                assert r.json()['data'] == [{'id':'allowed'}]
        finally:
            client.portal.call(app.state.client.aclose)
            app.state.client = original


@pytest.mark.parametrize('header', [b'Bearer sk-client-key', b'Bearer session-jwt'])
def test_chat_access_accepts_the_chat_apps_bearer_credential(tmp_path, monkeypatch, header):
    """API clients send the chat app's token or API key as a bearer and no
    cookie. The picker's access check validates it with Open WebUI instead of
    refusing it, as the model list did before the edge filtered it."""
    from fastapi import FastAPI
    from hubzoid.access import session

    monkeypatch.setattr(session, 'store_for', lambda _: __import__('unittest.mock').mock.Mock())
    monkeypatch.setattr(deployment, 'owui_url', lambda _: 'http://127.0.0.1:43080')
    seen = {}

    def fake_get(url, headers=None, **kw):
        seen['auth'] = (headers or {}).get('Authorization')
        return httpx.Response(200, json={'role': 'user', 'email': 'ana@example.org', 'id': 'u'})

    monkeypatch.setattr(httpx, 'get', fake_get)
    monkeypatch.setattr(portal, 'store_for', lambda _: __import__('unittest.mock').mock.Mock(
        is_suspended=lambda s: False, is_authoritative=lambda h: False))
    monkeypatch.setattr(deployment, 'hubs', lambda _: [])
    app = FastAPI()
    app.include_router(portal.build_router(tmp_path))
    r = TestClient(app).get('/portal/api/chat-access', headers={'authorization': header.decode()})
    assert r.status_code == 200 and r.json() == {'denied': [], 'blocked': False, 'allowed': 0}
    assert seen['auth'] == header.decode()
    # Without any credential it is still a sign-in refusal.
    assert TestClient(app).get('/portal/api/chat-access').status_code == 401


@pytest.mark.parametrize('suspended,granted,expected', [
    (False, {'a'}, {'denied': ['m-b'], 'blocked': False, 'allowed': 1}),
    (False, set(), {'denied': ['m-a', 'm-b'], 'blocked': False, 'allowed': 0}),
    (True, {'a', 'b'}, {'denied': ['m-a', 'm-b'], 'blocked': True, 'allowed': 0}),
])
def test_chat_access_says_why_the_agent_list_is_empty(tmp_path, monkeypatch, suspended, granted, expected):
    """The chat's notice tells a blocked person, and a person with no agents
    yet, why their list is empty, from the same answer that filters it."""
    from unittest import mock
    from fastapi import FastAPI

    monkeypatch.setattr(portal, '_verify_owui_session', lambda *a, **k: 'ana@example.org')
    monkeypatch.setattr(portal, 'store_for', lambda _: mock.Mock(
        is_suspended=lambda s: suspended, is_authoritative=lambda h: True,
        can=lambda s, h, p: h in granted))
    monkeypatch.setattr(deployment, 'hubs', lambda _: [{'key': 'a', 'model_id': 'm-a'},
                                                     {'key': 'b', 'model_id': 'm-b'}])
    app = FastAPI()
    app.include_router(portal.build_router(tmp_path))
    assert TestClient(app).get('/portal/api/chat-access').json() == expected


def test_bridges_without_the_gateway_environment_find_owner_and_public_url(tmp_path, monkeypatch):
    """Under `gateway --no-bridges`, bridges and CLI commands do not inherit the
    gateway's environment. The manifest carries the owner (so the Console works
    after an upgrade) and the public address (so report links from a CLI-run
    job point at the site the gateway serves)."""
    from hubzoid import artifacts
    from hubzoid.access import session

    hub = tmp_path / "sales"
    hub.mkdir()
    deployment.save(tmp_path / "deployment.json",
                    hubs=[dict(key="sales", name="Sales", path=str(hub), model_id="sales")],
                    operational_url=f"sqlite:///{tmp_path / 'op.db'}", owui_url="http://127.0.0.1:43080",
                    owui_db=str(tmp_path / "webui.db"), owner="Owner@Example.org ",
                    public_url="https://hub.example.org/")
    for key in ("HUBZOID_GATEWAY_ADMIN_EMAIL", "WEBUI_ADMIN_EMAIL", "HUBZOID_PUBLIC_URL",
                "WEBUI_URL", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WEBUI_AUTH", "true")
    assert session.configured_owner(hub) == "owner@example.org"
    assert artifacts.viewer_url("a1", hub) == "https://hub.example.org/portal/artifacts/a1"
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "env@example.org")   # the environment wins
    assert session.configured_owner(hub) == "env@example.org"
