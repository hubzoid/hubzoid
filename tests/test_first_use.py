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
