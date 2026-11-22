"""Linear delivery refusals, upload boundaries and ambiguous GraphQL results."""
from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from hubzoid.channels.errors import ChannelError
from hubzoid.channels.linear import adapter as mod
from hubzoid.channels.linear import oauth
from hubzoid.channels.models import Origin, Verified
from hubzoid.work_items.errors import Rejected
from tests.test_channels_linear import CLIENT, HOOK, ORG, TEAM, session_event


@pytest.fixture
def conn():
    settings = mod.LinearConfig(organization_id=ORG, team_ids=[TEAM], oauth_client_id=CLIENT,
                                client_secret_ref='env:CLIENT', webhook_secret_ref='env:HOOK')
    return SimpleNamespace(settings=settings, state={}, id='linear-main', hub='desk',
                           secret=lambda key: HOOK)


@pytest.mark.parametrize('teams,message', [(['ENG'], 'not a Linear team UUID'), ([TEAM, TEAM], 'lists a team twice')])
def test_team_ids_must_be_unique_uuids(conn, teams, message):
    with pytest.raises(ValidationError, match=message):
        mod.LinearConfig.model_validate({**conn.settings.model_dump(), 'team_ids': teams})


@pytest.mark.parametrize('raw,headers,code', [
    (b'{}', {}, 'signature_invalid'),
    (b'{}', {'linear-signature': 'wrong'}, 'signature_invalid'),
    (b'not json', {'signed': True}, 'invalid_request'),
    (b'{}', {'signed': True}, 'clock_skew'),
])
def test_invalid_deliveries_fail_before_parsing(conn, raw, headers, code):
    if headers.pop('signed', None):
        headers['linear-signature'] = hmac.new(HOOK.encode(), raw, hashlib.sha256).hexdigest()
    with pytest.raises(ChannelError) as exc:
        mod.LinearAdapter().verify(conn, raw, headers, {})
    assert exc.value.code == code


@pytest.mark.parametrize('delivery,app_user,code', [('', None, 'invalid_request'),
    ('x' * 201, None, 'invalid_request'), ('d1', 'another-app-user', 'installation_mismatch')])
def test_delivery_id_and_installation_are_bound(conn, delivery, app_user, code):
    conn.state['linear_app_user_id'] = 'app-user-1'
    payload = session_event()
    if app_user:
        payload['appUserId'] = app_user
    raw = json.dumps(payload).encode()
    headers = {'linear-delivery': delivery,
               'linear-signature': hmac.new(HOOK.encode(), raw, hashlib.sha256).hexdigest()}
    with pytest.raises(ChannelError) as exc:
        mod.LinearAdapter().verify(conn, raw, headers, {})
    assert exc.value.code == code


@pytest.mark.parametrize('payload', [{'type': 'Issue'}, {'type': 'AgentSessionEvent', 'agentSession': {}},
                                     {'type': 'AgentSessionEvent', 'agentSession': {'id': 7}}])
def test_unrelated_or_unaddressed_event_starts_no_work(conn, payload):
    assert mod.LinearAdapter().parse(conn, Verified('d', ORG, 'test', b'', payload)) == []


def test_no_human_actor_and_upload_limit(conn):
    payload = session_event(user=None, body='https://uploads.linear.app/one.txt https://uploads.linear.app/two.txt')
    conn.settings.upload_max_count = 1
    event = mod.LinearAdapter().parse(conn, Verified('d', ORG, 'test', b'', payload))[0]
    assert event.ignore_reason == 'no_human_actor' and event.actor is None
    assert event.files == [{'url': 'https://uploads.linear.app/one.txt', 'name': 'one.txt'}]


@pytest.mark.parametrize('status,data,write,error', [
    (401, {}, True, Rejected), (403, {}, False, Rejected),
    (400, {'errors': [{'message': 'invalid'}]}, True, Rejected),
    (200, {'errors': [{'message': 'bad', 'extensions': {'code': 'BAD_USER_INPUT'}}]}, True, Rejected),
    (500, {'errors': [{'message': 'unavailable'}]}, True, RuntimeError),
    (429, {}, True, RuntimeError), (200, {'errors': [{'message': 'unknown'}]}, True, RuntimeError),
])
def test_graphql_distinguishes_refused_from_uncertain(conn, monkeypatch, status, data, write, error):
    monkeypatch.setattr(oauth, 'access_token', lambda c: 'token')
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: httpx.Response(status, json=data))
    with pytest.raises(error, match='Linear'):
        mod.graphql(conn, 'mutation', write=write)


def test_graphql_without_token_and_empty_success(conn, monkeypatch):
    monkeypatch.setattr(oauth, 'current_token', lambda c: None)
    with pytest.raises(ChannelError) as exc:
        mod.graphql(conn, 'query', refresh=False)
    assert exc.value.code == 'auth_required'
    monkeypatch.setattr(oauth, 'current_token', lambda c: 'token')
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: httpx.Response(200))
    assert mod.graphql(conn, 'query', refresh=False) == {}


@pytest.mark.parametrize('url', ['http://uploads.linear.app/a', 'https://evil.example/a',
                                'https://user:pass@uploads.linear.app/a'])
def test_uploads_only_from_exact_https_host(conn, url):
    with pytest.raises(ChannelError) as exc:
        mod.LinearAdapter().fetch_file(conn, None, {'url': url})
    assert exc.value.code == 'attachment_blocked' and exc.value.status == 422


@pytest.mark.parametrize('status,mime,data,limit,code', [
    (302, 'text/plain', b'redirect', 10, 'attachment_unavailable'),
    (200, 'text/html', b'login', 10, 'attachment_unavailable'),
    (200, 'text/plain', b'12345', 4, 'attachment_too_large'),
    (200, 'text/plain', b'123', 4, None),
])
def test_upload_stream_bounds_and_sanitized_name(conn, monkeypatch, status, mime, data, limit, code):
    conn.settings.upload_max_bytes = limit
    monkeypatch.setattr(oauth, 'access_token', lambda c: 'token')
    seen = []

    @contextmanager
    def stream(*args, **kw):
        seen.append(kw)
        yield httpx.Response(status, headers={'content-type': mime}, content=data)

    monkeypatch.setattr(httpx, 'stream', stream)
    ref = {'url': 'https://uploads.linear.app/file', 'name': 'a/unsafe?.txt'}
    if code:
        with pytest.raises(ChannelError) as exc:
            mod.LinearAdapter().fetch_file(conn, None, ref)
        assert exc.value.code == code
    else:
        assert mod.LinearAdapter().fetch_file(conn, None, ref) == ('a_unsafe_.txt', mime, b'123')
    assert seen[0]['follow_redirects'] is False and seen[0]['headers']['Authorization'] == 'Bearer token'


@pytest.mark.parametrize('failure,expected', [('not found', False), ('temporary', None), ('other', None)])
def test_reconciliation_never_infers_delivery_from_errors(conn, monkeypatch, failure, expected):
    def broken(*a, **kw):
        raise RuntimeError(failure) if failure != 'other' else OSError('offline')

    monkeypatch.setattr(mod, 'graphql', broken)
    origin = Origin('linear', ORG, 'thread', {'agent_session_id': 's1'})
    assert mod.LinearAdapter().find(conn, origin, 'marker') is expected


def test_reconciliation_refuses_activity_from_another_session(conn, monkeypatch):
    monkeypatch.setattr(mod, 'graphql', lambda *a, **kw: {'agentActivity': {
        'id': 'a1', 'agentSession': {'id': 'another'}}})
    origin = Origin('linear', ORG, 'thread', {'agent_session_id': 's1'})
    assert mod.LinearAdapter().find(conn, origin, 'marker') is None
    monkeypatch.setattr(mod, 'graphql', lambda *a, **kw: {'agentActivityCreate': {'success': False}})
    outbound = SimpleNamespace(kind='result', marker='marker', text='Done')
    with pytest.raises(Rejected, match='did not accept'):
        mod.LinearAdapter().reply(conn, origin, outbound)


@pytest.mark.parametrize('failure,code', [('token refused', 'reauth_required'), ('offline', 'provider_unavailable')])
def test_doctor_explains_failed_token_check(conn, monkeypatch, failure, code):
    conn.state['linear_app_user_id'] = 'app-user-1'
    monkeypatch.setattr(oauth, 'current_token', lambda c: 'token')

    def broken(*a, **kw):
        raise RuntimeError(failure)

    monkeypatch.setattr(mod, 'graphql', broken)
    checks = mod.LinearAdapter().doctor(conn, {})
    assert checks[0].code == code and checks[0].status == 'fail'
