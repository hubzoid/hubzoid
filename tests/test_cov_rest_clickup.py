"""ClickUp refusal versus uncertainty, webhook teardown and reply failures."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from hubzoid.channels.clickup import adapter as mod
from hubzoid.channels.clickup import api
from hubzoid.channels.errors import ChannelError
from hubzoid.channels.models import Origin, Verified
from hubzoid.work_items.errors import Rejected
from tests.test_channels_clickup import LIST, TOKEN, WS


@pytest.fixture
def conn(tmp_path):
    return SimpleNamespace(hub_dir=tmp_path, hub='desk', id='clickup', state={},
        settings=mod.ClickUpConfig(workspace_id=WS, list_ids=[LIST], token_ref='env:TOKEN',
                                    trigger={'status': 'ready'}, statuses={'result': 'review'}),
        secret=lambda key: TOKEN)


@pytest.mark.parametrize('status,data,write,exception,code', [
    (401, {}, True, Rejected, None), (401, {}, False, ChannelError, 'reauth_required'),
    (400, {}, True, Rejected, None), (403, {}, False, ChannelError, 'permission_denied'),
    (404, {}, False, ChannelError, 'not_found'), (429, {}, True, RuntimeError, None),
    (500, {}, True, RuntimeError, None), (500, {}, False, ChannelError, 'provider_unavailable'),
    (400, {'ECODE': 'OAUTH_025', 'err': 'expired'}, False, ChannelError, 'reauth_required'),
])
def test_api_error_classification(conn, monkeypatch, status, data, write, exception, code):
    monkeypatch.setattr(httpx, 'request', lambda *a, **kw: httpx.Response(status, json=data))
    with pytest.raises(exception, match='ClickUp') as exc:
        api.call(conn, 'POST' if write else 'GET', '/test', write=write)
    if code:
        assert exc.value.code == code


@pytest.mark.parametrize('write,exception', [(False, ChannelError), (True, RuntimeError)])
def test_timeout_never_claims_write_was_refused(conn, monkeypatch, write, exception):
    def timeout(*a, **kw):
        raise httpx.ReadTimeout('timed out')

    monkeypatch.setattr(httpx, 'request', timeout)
    with pytest.raises(exception, match='did not answer'):
        api.call(conn, 'POST', '/test', write=write)


@pytest.mark.parametrize('response,expected', [(httpx.Response(200, json=[1, 2]), {'items': [1, 2]}),
    (httpx.Response(200, content=b'not JSON'), {}), (httpx.Response(204), {})])
def test_success_response_shapes(conn, monkeypatch, response, expected):
    monkeypatch.setattr(httpx, 'request', lambda *a, **kw: response)
    assert api.call(conn, 'GET', '/test') == expected


def test_malformed_attachment_sizes_are_ignored(conn, monkeypatch):
    monkeypatch.setattr(api, 'call', lambda *a, **kw: {'attachments': [
        {'title': 'ok.txt', 'size': '3'}, {'title': 'bad.txt', 'size': 'unknown'}, {'title': 'none'}]})
    assert api.attachments(conn, 't1') == {('ok.txt', 3), ('none', -1)}


@pytest.mark.parametrize('raw,headers,code', [(b'{}', {}, 'signature_invalid'),
    (b'bad', {'x-signature': 'x'}, 'invalid_request'), (b'[]', {'x-signature': 'x'}, 'invalid_request')])
def test_bad_delivery_shapes(conn, raw, headers, code):
    with pytest.raises(ChannelError) as exc:
        mod.ClickUpAdapter().verify(conn, raw, headers, {})
    assert exc.value.code == code


@pytest.mark.parametrize('payload', [{'event': 'unrelated', 'task_id': 't1'},
                                    {'event': 'taskCreated', 'task_id': '../escape'}])
def test_unrelated_or_invalid_task_ids_start_no_work(conn, payload):
    assert mod.ClickUpAdapter().parse(conn, Verified('d1', WS, 'test', payload=payload)) == []


def test_comments_without_people_are_not_requests(conn):
    payload = {'event': 'taskCommentPosted', 'task_id': 't1', 'history_items': [
        {'id': 'h1', 'comment': {'text_content': 'Hello'}}]}
    event = mod.ClickUpAdapter().parse(conn, Verified('d1', WS, 'test', payload=payload))[0]
    assert event.ignore_reason == 'no_human_actor' and event.trigger is False


def test_task_read_refusals_and_missing_task(conn, monkeypatch):
    payload = {'event': 'taskCreated', 'task_id': 't1'}
    event = mod.ClickUpAdapter().parse(conn, Verified('d1', WS, 'test', payload=payload))[0]

    def missing(*a):
        raise ChannelError('not_found', 'Gone', status=404)

    monkeypatch.setattr(api, 'get_task', missing)
    assert mod.ClickUpAdapter().authorize(conn, event) == 'task_unavailable'

    def unavailable(*a):
        raise ChannelError('provider_unavailable', 'Offline', status=503)

    monkeypatch.setattr(api, 'get_task', unavailable)
    with pytest.raises(ChannelError) as exc:
        mod.ClickUpAdapter().authorize(conn, event)
    assert exc.value.status == 503


def test_reply_keeps_posted_comment_when_status_fails(conn, monkeypatch, caplog):
    monkeypatch.setattr(api, 'post_comment', lambda *a: {'id': 'comment-1'})

    def broken(*a):
        raise RuntimeError('status missing')

    monkeypatch.setattr(api, 'set_status', broken)
    origin = Origin('clickup', WS, 'thread', {'task_id': 't1'})
    outbound = SimpleNamespace(kind='result', text='Done', marker='ref-1', files=[], part=1, parts=1)
    receipt = mod.ClickUpAdapter().reply(conn, origin, outbound)
    assert receipt.message_id == 'comment-1' and 'could not set status' in caplog.text
    monkeypatch.setattr(api, 'post_comment', lambda *a: {})
    with pytest.raises(RuntimeError, match="comment's id"):
        mod.ClickUpAdapter().reply(conn, origin, outbound)


def test_find_failure_stays_uncertain_and_teardown_records_successes(conn, monkeypatch, caplog):
    from hubzoid.channels import store

    def broken(*a):
        raise RuntimeError('offline')

    monkeypatch.setattr(api, 'comments', broken)
    origin = Origin('clickup', WS, 'thread', {'task_id': 't1'})
    assert mod.ClickUpAdapter().find(conn, origin, 'ref-1') is None
    conn.state['clickup_webhooks'] = {'good': {'list_id': LIST}, 'bad': {'list_id': LIST}}
    deleted, updates = [], []

    def delete(c, wid):
        if wid == 'bad':
            raise RuntimeError('offline')
        deleted.append(wid)

    monkeypatch.setattr(api, 'delete_webhook', delete)
    monkeypatch.setattr(store, 'set_connection_state', lambda *a, **kw: updates.append(kw))
    assert mod.teardown(conn) == {'deleted': ['good']}
    assert deleted == ['good'] and updates == [{'clickup_webhooks': {}}]
    assert 'could not delete webhook bad' in caplog.text


def test_setup_requires_public_url(conn, monkeypatch):
    monkeypatch.delenv('HUBZOID_PUBLIC_URL', raising=False)
    with pytest.raises(ChannelError) as exc:
        mod.setup(conn, actor='owner')
    assert exc.value.code == 'public_url_required' and exc.value.status == 422


@pytest.mark.parametrize('stage,code', [('token', 'reauth_required'), ('list', 'not_found'),
    ('webhooks', 'provider_unavailable')])
def test_doctor_reports_provider_failures(conn, monkeypatch, stage, code):
    monkeypatch.setattr(api, 'me', lambda *a: {'username': 'bot', 'id': 1})
    monkeypatch.setattr(api, 'get_list', lambda *a: {'name': 'Requests', 'statuses': [{'status': 'ready'}]})
    monkeypatch.setattr(api, 'webhooks', lambda *a: [])

    def broken(*a):
        raise ChannelError(code, 'Provider refused', status=503)

    monkeypatch.setattr(api, {'token': 'me', 'list': 'get_list', 'webhooks': 'webhooks'}[stage], broken)
    checks = mod.ClickUpAdapter().doctor(conn, {})
    assert any(c.status == 'fail' and c.code == code for c in checks)
