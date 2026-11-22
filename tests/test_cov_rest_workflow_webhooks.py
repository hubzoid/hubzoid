"""Webhook JSON, readiness and durable admission failures without an engine."""
from __future__ import annotations

import hashlib
import hmac

import pytest

from hubzoid.workflows import webhooks
from tests.test_workflow_webhook_delivery import _client


def test_unknown_or_invalid_declaration_is_refused(tmp_path, monkeypatch):
    client, path = _client(tmp_path, monkeypatch, {'verify': 'header'})
    out = client.post(path.replace('/ticket', '/unknown'), json={})
    assert out.status_code == 404 and out.json()['detail'] == 'Unknown webhook'

    def invalid(*a):
        raise ValueError('bad settings')

    monkeypatch.setattr(webhooks.events, 'declarations', invalid)
    out = client.post(path, json={})
    assert out.status_code == 503 and out.json()['detail'] == 'Workflow settings are invalid'


@pytest.mark.parametrize('length,status,message', [('-1', 413, 'too large'), ('many', 400, 'Invalid content length')])
def test_content_length_is_bounded(tmp_path, monkeypatch, length, status, message):
    client, path = _client(tmp_path, monkeypatch, {'verify': 'header'})
    out = client.post(path, content=b'{}', headers={'content-length': length})
    assert out.status_code == status and message in out.json()['detail']


@pytest.mark.parametrize('raw,status,message', [(b'not json', 400, 'Expected JSON'),
    (b'[]', 422, 'Expected a JSON object')])
def test_authenticated_body_must_be_json_object(tmp_path, monkeypatch, raw, status, message):
    client, path = _client(tmp_path, monkeypatch, {'verify': 'header'})
    out = client.post(path, content=raw, headers={'x-hook-secret': 'test-secret'})
    assert out.status_code == status and out.json()['detail'] == message


def test_absent_engine_does_not_accept_delivery(tmp_path, monkeypatch):
    client, path = _client(tmp_path, monkeypatch, {'verify': 'header'})
    monkeypatch.setattr(webhooks, 'readiness', lambda *a: {})
    out = client.post(path, json={}, headers={'x-hook-secret': 'test-secret'})
    assert out.status_code == 503 and out.json()['detail'] == 'Workflow engine is not ready'


@pytest.mark.parametrize('failure,status,message', [(ValueError, 409, 'Event key content mismatch'),
    (OSError, 503, 'Could not durably accept event; retry delivery')])
def test_admission_failures_ask_sender_to_retry(tmp_path, monkeypatch, failure, status, message):
    client, path = _client(tmp_path, monkeypatch, {'verify': 'header'})

    def failed(*a):
        raise failure('private detail')

    monkeypatch.setattr(webhooks.events, 'admit', failed)
    out = client.post(path, json={}, headers={'x-hook-secret': 'test-secret'})
    assert out.status_code == status and out.json()['detail'] == message


def test_github_verifies_exact_bytes_and_missing_secret_fails_closed(monkeypatch):
    raw = b'{"id":1}'
    monkeypatch.setenv('WEBHOOK_SECRET_TICKET', 'secret')
    signature = 'sha256=' + hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
    assert webhooks.verify(raw, {'x-hub-signature-256': signature}, 'ticket', {'verify': 'github'}) is True
    assert webhooks.verify(raw + b' ', {'x-hub-signature-256': signature}, 'ticket', {'verify': 'github'}) is False
    monkeypatch.delenv('WEBHOOK_SECRET_TICKET')
    assert webhooks.verify(raw, {}, 'ticket', {}) is False


def test_event_key_fallback_does_not_collide_on_delimiters():
    spec = {'event_key': '{a}:{b}'}
    assert webhooks.key_for({'a': 'x:y', 'b': 'z'}, {}, spec) != webhooks.key_for({'a': 'x', 'b': 'y:z'}, {}, spec)
    assert webhooks.field({'a': 1}, 'a.b') is None
    assert webhooks.key_for({}, {'x-delivery-id': 'delivery-1'}, spec) == 'id:delivery-1'
