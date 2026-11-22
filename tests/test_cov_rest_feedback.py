"""Feedback validation, notification ownership and pinned webhook transport."""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx
import pytest

from hubzoid.chat import config
from hubzoid.chat import feedback as fb
from tests.test_cov_workflows_context import hub  # noqa: F401


@pytest.fixture
def svc(hub):  # noqa: F811
    from hubzoid.db import operational_engine

    return fb.service_for(hub, operational_engine(hub), hub.name)


@pytest.mark.parametrize('key,value,enabled,code', [
    ('desktop', 'yes', True, 'validation_failed'), ('unknown', True, True, 'validation_failed'),
    ('sound', True, False, 'policy_locked'), ('delivery', 'sometimes', True, 'validation_failed'),
    ('preview', 'body', True, 'validation_failed'),
])
def test_personal_notification_policy(key, value, enabled, code):
    with pytest.raises(config.ConfigError) as exc:
        fb._personal_notifications(key, value, {'notifications': {'enabled': enabled}})
    assert exc.value.code == code


@pytest.mark.parametrize('key,value,code', [('enabled.unknown', True, 'validation_failed'),
    ('enabled.titles', 'yes', 'validation_failed'), ('enabled.titles', True, 'policy_locked')])
def test_helper_policy_cannot_be_widened(key, value, code):
    with pytest.raises(config.ConfigError) as exc:
        fb._personal_helpers(key, value, {'helpers': {'enabled': {'titles': False}}})
    assert exc.value.code == code


@pytest.mark.parametrize('body,message', [
    ({'rating': 'other'}, 'rating is positive or negative'),
    ({'reason_ids': ['missing_source'] * 9}, 'reason_ids are up to 8'),
    ({'tags': [42]}, 'tags are up to 10'),
    ({'comment': 42}, 'comment is up to'),
    ({'score': True}, 'score is a whole number'),
])
def test_rating_fields_have_specific_refusals(svc, body, message):
    with pytest.raises(fb.Err, match=message) as exc:
        svc._content({'rating': 'negative', **body})
    assert exc.value.code == 'validation_failed' and exc.value.status == 422


@pytest.mark.parametrize('scope,manager,code', [('hub', False, 'permission_denied'),
    ('global', True, 'validation_failed')])
def test_target_scope_cannot_escape_person_or_hub(svc, scope, manager, code):
    with pytest.raises(fb.Err) as exc:
        svc.scope_of(scope, 'a', manager=manager)
    assert exc.value.code == code


@pytest.mark.parametrize('target_id,body,message', [
    ('document', {'type': 'email'}, 'Target ids'), ('bad/id', {'type': 'email'}, 'Target ids'),
    ('ops', {'type': 'email', 'events': ['unknown']}, 'events are among'),
    ('ops', {'type': 'email', 'events': 'run.failed'}, 'events are among'),
])
def test_bad_target_never_stored(svc, target_id, body, message):
    with pytest.raises(fb.Err, match=message):
        svc.put_target('hub:desk', target_id, body, owner_id=None, expected_revision='absent')
    assert svc.list_targets('hub:desk') == {'scope_revision': 0, 'items': []}


def test_non_webhook_target_cannot_be_verified(svc):
    made = svc.put_target('hub:desk', 'ops', {'type': 'email', 'recipient_ref': 'a@example.org'},
                          owner_id=None, expected_revision='absent')
    assert made['deliverable'] is False and made['verified'] is False
    with pytest.raises(fb.Err) as exc:
        svc.verify_target('hub:desk', 'ops')
    assert exc.value.status == 503 and exc.value.code == 'unavailable'
    with pytest.raises(fb.Err) as exc:
        svc.verify_target('hub:desk', 'missing')
    assert exc.value.code == 'not_found'
    with pytest.raises(fb.Err) as exc:
        svc.delete_target('hub:desk', 'missing', 1)
    assert exc.value.code == 'not_found'


def test_webhook_verification_refusal_and_destination_race(svc, monkeypatch):
    monkeypatch.setattr(fb, 'check_webhook_url', lambda url: '93.184.216.34')
    body = {'type': 'webhook', 'recipient_ref': 'https://hooks.example.org/one', 'connection_ref': 'env:HOOK'}
    made = svc.put_target('hub:desk', 'ops', body, owner_id=None, expected_revision='absent')
    with pytest.raises(fb.Err, match='http_403') as exc:
        svc.verify_target('hub:desk', 'ops', send=lambda *a: (False, 'http_403'))
    assert exc.value.code == 'target_unverified'

    def changed(*a):
        svc.put_target('hub:desk', 'ops', {**body, 'recipient_ref': 'https://hooks.example.org/two'},
                        owner_id=None, expected_revision=made['revision'])
        return True, 'http_200'

    with pytest.raises(fb.Err, match='changed while') as exc:
        svc.verify_target('hub:desk', 'ops', send=changed)
    assert exc.value.code == 'revision_conflict'
    assert svc.list_targets('hub:desk')['items'][0]['verified'] is False


def test_request_recipients_use_accounts_and_never_guess(svc, monkeypatch):
    from hubzoid.auth import users

    def find(email):
        if email == 'broken@example.org':
            raise OSError('offline')
        return {'id': 'u_alice'} if email == 'alice@example.org' else None

    monkeypatch.setattr(users, 'store', lambda *a: SimpleNamespace(find_by_email=find))
    assert svc._request_recipients({'requester_id': 'u_requester', 'approvers': [
        'requester', ' ALICE@example.org ', 'broken@example.org', 'unknown@example.org', 'u_bob', '']}) == {
            'u_requester', 'u_alice', 'u_bob'}


@pytest.mark.parametrize('state,code', [('unread', 'validation_failed'), ('read', 'not_found')])
def test_notification_mark_checks_state_and_owner(svc, state, code):
    with pytest.raises(fb.Err) as exc:
        svc.mark('other', 'missing', state)
    assert exc.value.code == code
    with pytest.raises(fb.Err, match='device_id is required'):
        svc.presence('a', {})


@pytest.mark.parametrize('url', ['http://example.org/hook', 'https://u:p@example.org/hook', 'missing-host'])
def test_webhook_requires_https_without_credentials(url):
    with pytest.raises(fb.Err, match='https:// URL'):
        fb.check_webhook_url(url)


def test_unresolvable_webhook_is_refused(monkeypatch):
    def missing(*a, **kw):
        raise fb.socket.gaierror('no host')

    monkeypatch.setattr(fb.socket, 'getaddrinfo', missing)
    with pytest.raises(fb.Err, match='does not resolve'):
        fb.check_webhook_url('https://example.org/hook')


@pytest.mark.parametrize('status,failure,expected', [(202, None, (True, 'http_202')),
    (503, None, (False, 'http_503')), (None, 'connect', (False, 'ConnectError')),
    (None, 'timeout', None)])
def test_signed_webhook_uses_pinned_address_and_original_tls_name(monkeypatch, status, failure, expected):
    monkeypatch.setattr(fb, 'check_webhook_url', lambda url: '2001:4860:4860::8888')
    monkeypatch.setenv('REST_TEST_HOOK', 'test-signing-secret')
    monkeypatch.setattr(fb.time, 'time', lambda: 1234)
    seen = []

    class Client:
        def __init__(self, **kw):
            seen.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            seen.append((url, kw))
            if failure == 'timeout':
                raise httpx.ReadTimeout('uncertain')
            if failure == 'connect':
                raise httpx.ConnectError('offline')
            return httpx.Response(status)

    monkeypatch.setattr(httpx, 'Client', Client)
    target = SimpleNamespace(recipient_ref='https://hooks.example.org:8443/path', connection_ref='env:REST_TEST_HOOK')
    payload = {'z': 2, 'a': 1}
    if failure == 'timeout':
        with pytest.raises(TimeoutError):
            fb.send_webhook(target, payload)
    else:
        assert fb.send_webhook(target, payload) == expected
    assert seen[0] == {'timeout': 10, 'follow_redirects': False}
    url, request = seen[1]
    assert url == 'https://[2001:4860:4860::8888]:8443/path'
    assert request['extensions'] == {'sni_hostname': 'hooks.example.org'}
    assert request['headers']['host'] == 'hooks.example.org:8443'
    assert json.loads(request['content']) == payload
    expected_sig = hmac.new(b'test-signing-secret', b'1234.' + request['content'], hashlib.sha256).hexdigest()
    assert request['headers']['x-hubzoid-signature'] == 'v1=' + expected_sig


def test_missing_signing_secret_sends_nothing(monkeypatch):
    monkeypatch.setattr(fb, 'check_webhook_url', lambda url: '93.184.216.34')
    monkeypatch.delenv('REST_TEST_HOOK', raising=False)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: pytest.fail('must not send unsigned webhook'))
    assert fb.send_webhook(SimpleNamespace(recipient_ref='https://example.org',
                          connection_ref='env:REST_TEST_HOOK'), {}) == (False, 'signing_secret_missing')
