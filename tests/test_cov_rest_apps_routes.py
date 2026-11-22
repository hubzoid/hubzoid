"""Card HTTP permissions, host routing and typed service-error responses."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from hubzoid.apps import cards, routes
from hubzoid.work_items import service as wi_service
from hubzoid.work_items.errors import WorkItemError
from tests.test_mcp_apps import OWNER, hub  # noqa: F401


@pytest.fixture
def api(hub, monkeypatch):  # noqa: F811
    actor = SimpleNamespace(subject=OWNER, surface='console', via='session')

    def hub_path(actor, key):
        if key != hub.name:
            raise KeyError(key)
        return hub

    monkeypatch.setattr(wi_service, '_can', lambda *a: True)
    monkeypatch.setattr(routes, '_request_identity', lambda *a: 'verified-request')
    router = APIRouter(prefix='/portal/api')
    routes.register(router, lambda: actor, hub_path)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client.headers['origin'] = 'http://testserver'
    return client, f'/portal/api/hubs/{hub.name}/apps'


def test_card_management_needs_known_hub_and_manage_access(api, monkeypatch):
    client, base = api
    out = client.get(base.replace('/cardhub/', '/missing/') + '/config')
    assert out.status_code == 404 and out.json()['detail']['code'] == 'hub_not_found'
    monkeypatch.setattr(wi_service, '_can', lambda *a: False)
    out = client.get(base + '/config')
    assert out.status_code == 403 and out.json()['detail']['code'] == 'permission_denied'


def test_settings_validate_and_probe_return_checked_configuration(api):
    client, base = api
    out = client.get(base + '/config')
    assert out.status_code == 200 and out.json()['revision'] == 1
    out = client.post(base + '/validate')
    assert out.status_code == 200 and out.json()['valid'] is True
    out = client.post(base + '/probe', json={'client_capabilities': {
        'extensions': {'io.modelcontextprotocol/ui': {}}}})
    assert out.status_code == 200 and out.json()['mode'] == 'card'
    out = client.get(base + '/instances')
    assert out.status_code == 200 and out.json() == {'instances': []}


def test_invoke_needs_idempotency_key_and_unknown_card_is_not_found(api):
    client, base = api
    out = client.post(base + '/invoke', json={'card_id': 'missing'})
    assert out.status_code == 400 and out.json()['detail']['message'] == 'Send an Idempotency-Key.'
    out = client.post(base + '/invoke', json={'card_id': 'missing'}, headers={'Idempotency-Key': 'invoke-1'})
    assert out.status_code == 404 and out.json()['detail']['code'] == 'not_found'


@pytest.mark.parametrize('server_id', [None, '', 123])
def test_host_requires_instance_routing_key(api, server_id):
    client, base = api
    out = client.post(base + '/host', json={'method': 'resources/list', 'params': {'serverId': server_id}})
    assert out.status_code == 400 and out.json()['detail']['message'] == 'Name the card (serverId).'


def test_host_consumes_routing_key_and_uses_admitted_identity(api, monkeypatch):
    client, base = api
    seen = []

    async def dispatch(*a, **kw):
        seen.append((a, kw))
        return {'resources': []}

    monkeypatch.setattr(cards, 'dispatch', dispatch)
    out = client.post(base + '/host', json={'method': 'resources/list', 'params': {'serverId': 'instance-1'}})
    assert out.status_code == 200 and out.json() == {'resources': []}
    assert seen[0][0][1:] == ('instance-1', OWNER, 'resources/list', {})
    assert seen[0][1]['identity'] == 'verified-request'


def test_dispatch_and_revoke_preserve_service_refusals(api, monkeypatch):
    client, base = api

    async def denied(*a, **kw):
        raise WorkItemError('revoked', 'Card revoked', status=410)

    monkeypatch.setattr(cards, 'dispatch', denied)
    out = client.post(base + '/instances/one/dispatch', json={'method': 'tools/call'})
    assert out.status_code == 410 and out.json()['detail'] == {'code': 'revoked', 'message': 'Card revoked'}
    out = client.post(base + '/instances/one/revoke', json={'reason': 'Old card'})
    assert out.status_code == 404 and out.json()['detail']['code'] == 'not_found'
    out = client.get(base + '/instances/one')
    assert out.status_code == 404 and out.json()['detail']['code'] == 'not_found'


@pytest.mark.parametrize('result,code', [({'refused': 'tool_unknown', 'message': 'Tool removed'}, 'tool_unknown'),
    ({'text': 'Plain result', 'card': None}, None)])
def test_invoke_tool_refusal_or_plain_fallback(hub, monkeypatch, result, code):  # noqa: F811
    import asyncio

    from hubzoid import mcp_server

    async def call(*a, **kw):
        return result

    monkeypatch.setattr(mcp_server, 'call_registry_tool', call)
    body = routes.InvokeBody(card_id='order-summary')
    if code:
        with pytest.raises(WorkItemError) as exc:
            asyncio.run(routes._invoke(hub, OWNER, body, identity='verified'))
        assert exc.value.code == code and exc.value.status == 404
    else:
        assert asyncio.run(routes._invoke(hub, OWNER, body, identity='verified')) == {
            'instance': None, 'text': 'Plain result', 'card': False}
