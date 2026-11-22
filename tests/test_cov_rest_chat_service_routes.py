"""Feedback and retrieval HTTP parsing, ownership and bounded fetch responses."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid.chat import feedback_routes, retrieval_routes
from hubzoid.control import authz
from tests.chat_helpers import ORIGIN
from tests.test_chat_link import chat  # noqa: F401


@pytest.fixture
def api(chat, monkeypatch):  # noqa: F811
    hub, engine, actor, link = chat

    async def user(request):
        return actor

    async def may_use(user):
        return None

    monkeypatch.setattr(authz, 'principal_for', lambda *a: SimpleNamespace(can=lambda *a: True))
    ctx = SimpleNamespace(hub_dir=hub, hub=hub.name, engine=engine, store=engine.store,
                          user=user, mutation=user, may_use_hub=may_use)
    app = FastAPI()
    feedback_routes.mount(app, ctx)
    retrieval_routes.mount(app, ctx)
    return TestClient(app), ctx, link, actor


@pytest.mark.parametrize('query,message', [('limit=many', 'limit is a number.'),
    ('since=yesterday', 'since is a time in seconds.')])
def test_feedback_filter_errors_are_named(api, query, message):
    client, _, _, _ = api
    out = client.get('/api/chat/feedback?' + query)
    assert out.status_code == 400 and out.json()['detail']['message'] == message


def test_feedback_export_requires_known_format(api):
    client, _, _, _ = api
    out = client.get('/api/chat/feedback/export?format=xml')
    assert out.status_code == 422 and out.json()['detail']['message'] == 'format is json or csv.'


def test_unavailable_principal_refuses_ratings_and_hub_targets(api, monkeypatch):
    client, _, _, _ = api

    def unavailable(*args):
        raise OSError('account store down')

    monkeypatch.setattr(authz, 'principal_for', unavailable)
    out = client.get('/api/chat/feedback')
    assert out.status_code == 403 and out.json()['detail']['code'] == 'permission_denied'
    out = client.get('/api/chat/notification-targets?scope=hub')
    assert out.status_code == 403 and out.json()['detail']['code'] == 'permission_denied'


@pytest.mark.parametrize('title', ['', 'x' * 201])
def test_title_limits_are_enforced_before_store_write(api, title):
    client, _, link, _ = api
    out = client.patch(f"/api/conversations/{link['conversation_id']}/title", headers=ORIGIN, json={'title': title})
    assert out.status_code == 422 and out.json()['detail']['message'] == 'A title is 1 to 200 characters.'


def test_only_owner_may_rename_and_helpers_read_their_branch(api, monkeypatch):
    client, ctx, link, _ = api
    out = client.get('/api/chat/helpers', params={'conversation_id': link['conversation_id'],
                                                'branch_id': link['branch_id']})
    assert out.status_code == 200 and out.json() == {'items': []}
    monkeypatch.setattr(ctx.engine, 'readable', lambda *a: ({'id': link['conversation_id']}, 'editor'))
    out = client.patch(f"/api/conversations/{link['conversation_id']}/title", headers=ORIGIN, json={'title': 'New'})
    assert out.status_code == 403 and out.json()['detail']['message'] == 'Only the owner can rename this conversation.'


def test_target_delete_and_delivery_history(api):
    client, ctx, _, actor = api
    scope = ctx.feedback.scope_of('own', actor.id, manager=False)
    created = ctx.feedback.put_target(scope, 'ops', {'type': 'email', 'recipient_ref': 'a@example.org'},
                                      owner_id=actor.id, expected_revision='absent')
    out = client.get('/api/chat/notification-deliveries?target_id=ops')
    assert out.status_code == 200 and out.json() == {'items': []}
    out = client.request('DELETE', '/api/chat/notification-targets/ops', headers=ORIGIN,
                          json={'expected_revision': created['revision']})
    assert out.status_code == 204 and ctx.feedback.list_targets(scope)['items'] == []


def test_retrieval_job_operation_and_unknown_job(api):
    client, _, _, _ = api
    out = client.post('/api/chat/retrieval/collections/c1/jobs', headers=ORIGIN, json={'operation': 'delete'})
    assert out.status_code == 422 and out.json()['detail']['message'] == 'operation is sync or reindex.'
    out = client.get('/api/chat/retrieval/jobs/missing')
    assert out.status_code == 404 and out.json()['detail']['code'] == 'not_found'


def test_checked_web_fetch_returns_bounded_excerpt(api, monkeypatch):
    client, ctx, _, _ = api
    cfg = {'enabled': True, 'web': {'enabled': True, 'search': 'native', 'limits': {'max_bytes': 5000},
                                   'allowed_domains': ['docs.example.org'], 'blocked_domains': []}}
    seen = []
    monkeypatch.setattr(ctx.retrieval, 'settings', lambda: cfg)
    monkeypatch.setattr(ctx.retrieval, 'check_fetch', lambda actor: None)
    data = b'Useful text ' * 250
    monkeypatch.setattr(ctx.retrieval, 'fetch', lambda *a, **kw: seen.append((a, kw)) or (
        'https://docs.example.org/final.txt', 'text/plain', data))
    out = client.post('/api/chat/retrieval/web/fetch', headers=ORIGIN,
                       json={'url': 'https://docs.example.org/start.txt'})
    assert out.status_code == 200 and out.json()['bytes'] == len(data)
    assert out.json()['url'] == 'https://docs.example.org/final.txt' and len(out.json()['excerpt']) == 2000
    assert out.json()['excerpt'].startswith('Useful text')
    assert seen == [(('https://docs.example.org/start.txt', {'max_bytes': 5000}),
                     {'allowed': ['docs.example.org'], 'blocked': []})]
    out = client.get('/api/chat/retrieval/health')
    assert out.status_code == 200 and out.json()['search'] == 'fts5-bm25' and out.json()['vector_store'] is None
