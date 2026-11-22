"""Card result and dispatch boundaries, live state and file reload failures."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hubzoid.apps import cards, config, kinds
from hubzoid.work_items.errors import WorkItemError
from tests.test_mcp_apps import OWNER, hub  # noqa: F401


@pytest.fixture
def card():
    return config.Card('custom', 'Custom', 'custom', 'lookup', 'hub', None,
                       ['model', 'app'], ['lookup'], 'abc', '<html>safe</html>')


@pytest.mark.parametrize('result,message', [
    ('not json', 'did not return JSON'),
    ({'n': float('nan')}, 'not finite'),
    ({'n': object()}, 'not plain JSON'),
    ({'text': 'x' * 1100}, 'limit is 1024'),
    ([1], 'must be a JSON object'),
])
def test_invalid_custom_results(card, result, message, tmp_path):
    with pytest.raises(kinds.Invalid, match=message):
        cards.check_result(tmp_path, card, result, config.Limits(result_bytes=1024))


def test_custom_result_schema_is_enforced(card, tmp_path):
    card.schema = {'type': 'object', 'properties': {'count': {'type': 'integer'}}}
    with pytest.raises(kinds.Invalid, match='output schema') as exc:
        cards.check_result(tmp_path, card, '{"count":"many"}', config.Limits())
    assert exc.value.pointer == '/count'
    assert cards.check_result(tmp_path, card, '{"count":3}', config.Limits()) == {'count': 3}


@pytest.fixture
def frame(tmp_path, monkeypatch, card):
    cards._rate.clear()
    cards._inflight.clear()
    inst = {'id': 'one', 'requester': OWNER, 'card_id': card.id, 'kind': 'custom', 'tool': 'lookup',
            'server_id': 'hub', 'created': 1, 'config_revision': 1, 'resource_sha256': card.sha256,
            'revoked': False, 'revoke_reason': None, 'result': {'title': 'Old'}, 'text': 'Old'}
    res = config.Resolved(True, True, cards={card.id: card})
    monkeypatch.setattr(cards.store, 'get_instance', lambda *a: inst)
    monkeypatch.setattr(cards, 'active', lambda *a, **kw: ({'revision': 1}, res))
    monkeypatch.setattr(cards.store, 'revision_config', lambda *a: res.stored())
    monkeypatch.setattr(cards.store, 'resource', lambda *a: card.html)
    audit = []
    monkeypatch.setattr(cards, '_audit', lambda *a: audit.append(a))
    calls = []

    async def call(name, args, identity, classes):
        calls.append((name, args, identity, classes))
        return {'text': '{"title":"New"}'}

    def dispatch(method, params=None, caller=OWNER, fn=call):
        return asyncio.run(cards.dispatch(tmp_path, 'one', caller, method, params or {}, identity='verified',
                                          call_tool=fn))

    yield SimpleNamespace(hub=tmp_path, inst=inst, res=res, card=card, audit=audit, calls=calls,
                          dispatch=dispatch)
    cards._rate.clear()
    cards._inflight.clear()


@pytest.mark.parametrize('key', ['hub', 'hubId', 'caller', 'user', 'server_url'])
def test_frames_cannot_supply_identity_or_hub(frame, key):
    with pytest.raises(WorkItemError) as exc:
        frame.dispatch('tools/call', {key: 'forged'})
    assert exc.value.code == 'invalid_request' and exc.value.message == f'{key} is not accepted from a card.'
    assert frame.calls == []


def test_other_viewer_and_revoked_frame_are_refused(frame):
    with pytest.raises(WorkItemError) as exc:
        frame.dispatch('resources/list', caller='other@example.org')
    assert exc.value.code == 'not_found'
    frame.inst['revoked'] = True
    with pytest.raises(WorkItemError) as exc:
        frame.dispatch('resources/list')
    assert exc.value.code == 'revoked' and exc.value.status == 410
    assert frame.calls == []


def test_frame_reads_only_its_retained_resource(frame, monkeypatch):
    own = f'ui://{frame.hub.name}/custom/abc/index.html'
    out = frame.dispatch('resources/list')
    assert out == {'resources': [{'uri': own, 'name': 'custom', 'mimeType': config.MIME}]}
    out = frame.dispatch('mcp-apps/read-resource')
    assert out['html'] == '<html>safe</html>' and out['uri'] == own
    assert frame.dispatch('resources/read')['contents'][0]['text'] == '<html>safe</html>'
    with pytest.raises(WorkItemError) as exc:
        frame.dispatch('resources/read', {'uri': 'ui://someone/secret'})
    assert exc.value.code == 'forbidden'
    monkeypatch.setattr(cards.store, 'resource', lambda *a: None)
    with pytest.raises(WorkItemError) as exc:
        frame.dispatch('resources/read')
    assert exc.value.code == 'not_found'


@pytest.mark.parametrize('method,params,code', [
    ('tools/list', {}, 'method_not_allowed'),
    ('tools/call', {'name': 'lookup', 'arguments': [1]}, 'invalid_request'),
    ('tools/call', {'name': 'send_email'}, 'forbidden'),
])
def test_dispatch_refusals(frame, method, params, code):
    with pytest.raises(WorkItemError) as exc:
        frame.dispatch(method, params)
    assert exc.value.code == code and frame.calls == []
    if code == 'forbidden':
        assert frame.audit[0][-1] == {'tool': 'send_email', 'reason': 'not_allowed'}


def test_changed_resource_stops_tool_calls_but_keeps_reads(frame):
    frame.card.sha256 = 'new'
    with pytest.raises(WorkItemError) as exc:
        frame.dispatch('tools/call', {'name': 'lookup'})
    assert exc.value.code == 'card_inactive' and exc.value.status == 409
    assert frame.dispatch('resources/read')['contents'][0]['text'] == '<html>safe</html>'
    assert frame.calls == []


@pytest.mark.parametrize('outcome,code', [
    ({'refused': 'external_write', 'message': 'Read tools only'}, 'forbidden'),
    ({'text': '[access denied: missing use_hub]'}, None),
])
def test_tool_refusals_keep_the_reason(frame, outcome, code):
    async def refused(*args):
        return outcome

    if code:
        with pytest.raises(WorkItemError) as exc:
            frame.dispatch('tools/call', {'name': 'lookup'}, fn=refused)
        assert exc.value.code == code and exc.value.message == 'Read tools only'
    else:
        assert frame.dispatch('tools/call', {'name': 'lookup'}, fn=refused) == {
            'content': [{'type': 'text', 'text': outcome['text']}], 'isError': True}
    assert cards._inflight[(OWNER, 'one')] == 0
    assert frame.audit[0][2] == 'apps.denied'


@pytest.mark.parametrize('text,structured', [('{"title":"New"}', {'title': 'New'}), ('plain text', None)])
def test_tool_result_checked_and_inflight_released(frame, text, structured):
    async def reply(*args):
        frame.calls.append(args)
        return {'text': text}

    out = frame.dispatch('tools/call', {'name': 'lookup', 'arguments': {'id': 3}}, fn=reply)
    assert out['content'] == [{'type': 'text', 'text': text}]
    assert out.get('structuredContent') == structured
    assert frame.calls == [('lookup', {'id': 3}, 'verified', cards.CALLABLE_CLASSES)]
    assert cards._inflight[(OWNER, 'one')] == 0


def test_failed_tool_releases_concurrency_slot(frame):
    async def failure(*args):
        raise RuntimeError('tool failed')

    with pytest.raises(RuntimeError, match='tool failed'):
        frame.dispatch('tools/call', {'name': 'lookup'}, fn=failure)
    assert cards._inflight[(OWNER, 'one')] == 0


def test_concurrency_limit_refuses_without_a_tool_call(frame):
    cards._inflight[(OWNER, 'one')] = 4
    with pytest.raises(WorkItemError) as exc:
        frame.dispatch('resources/list')
    assert exc.value.code == 'rate_limited' and exc.value.fields['retry_after'] == 2
    assert frame.calls == []


@pytest.mark.parametrize('caps,mode', [
    (None, 'text'), ({'extensions': []}, 'text'),
    ({'extensions': {'io.modelcontextprotocol/ui': {}}}, 'card'),
    ({'extensions': {'io.modelcontextprotocol/ui': {'mimeTypes': ['other']}}}, 'text'),
    ({'extensions': {'io.modelcontextprotocol/ui': {'mimeTypes': [config.MIME]}}}, 'card'),
])
def test_probe_negotiates_declared_mime(frame, caps, mode):
    out = cards.probe(frame.hub, caps)
    assert out['mode'] == mode and out['active_revision'] == 1
    assert out['cards'] == [{'card_id': 'custom', 'tool': 'lookup', 'mode': mode, 'text_fallback': True}]


def test_progress_and_approval_use_authoritative_state(frame, monkeypatch):
    from hubzoid.work_items import service, store

    monkeypatch.setattr(service, 'show', lambda *a: {'id': 'w1', 'state': 'succeeded', 'reason': 'Finished',
                                                   'workflow': 'daily', 'requester': OWNER, 'updated': 7})
    monkeypatch.setattr(service, 'operation_detail', lambda *a: {'name': 'send', 'item_id': 'w1',
                         'state': 'pending', 'args_digest': 'new', 'created': 2, 'approval': 'required'})
    monkeypatch.setattr(store, 'get_item', lambda *a: {'requester': OWNER})
    monkeypatch.setattr(cards, 'link', lambda h, i: f'/work/{i}')
    out = cards.status(frame.hub, {'kind': 'progress', 'work_item_id': 'w1'}, OWNER)
    assert out['summary'] == 'Finished' and out['facts']['updated'] == 7
    out = cards.status(frame.hub, {'kind': 'approval', 'operation_id': 'o1', 'digest': 'old'}, OWNER)
    assert out['state'] == 'changed' and out['label'] == 'changed since this card was made'
    assert out['facts']['requester'] == OWNER and out['link'] == '/work/w1'


def test_missing_status_is_safe_and_never_fabricated(frame, monkeypatch):
    frame.inst['result']['kind'] = 'custom'
    assert cards._status_result(frame.hub, frame.inst, OWNER) == {
        'content': [{'type': 'text', 'text': 'This card has no live state.'}], 'isError': True}
    frame.inst['kind'] = 'progress'

    def gone(*args):
        raise WorkItemError('not_found', 'No work visible', status=404)

    monkeypatch.setattr(cards, 'status', gone)
    assert cards.view(frame.hub, 'one', OWNER)['status'] == {
        'state': 'unavailable', 'label': 'not available', 'error': 'not_found'}
    assert cards._status_result(frame.hub, frame.inst, OWNER)['content'][0]['text'] == 'No work visible'
    frame.inst['revoked'] = True
    assert cards._status_result(frame.hub, frame.inst, OWNER)['content'][0]['text'] == 'This card was revoked.'


def test_bad_file_reload_keeps_last_good_revision(hub, monkeypatch):  # noqa: F811
    initial = cards.get(hub)
    (hub / config.FILE).write_text('version: 1\nunknown: true\n')
    assert cards.get(hub)['revision'] == initial['revision']
    (hub / config.FILE).write_text('version: 1\nenabled: false\n')

    def unreadable(*a):
        raise OSError('disk unavailable')

    monkeypatch.setattr(cards, 'validate', unreadable)
    assert cards.get(hub)['revision'] == initial['revision']


def test_wrapped_tool_survives_presentation_failure(card, tmp_path, monkeypatch):
    async def original(ctx, args):
        return 'original result'

    def fail(*args, **kw):
        raise OSError('storage unavailable')

    monkeypatch.setattr(cards, 'present', fail)
    tool = SimpleNamespace(name='lookup', on_invoke_tool=original)
    wrapped = cards._wrapped(tmp_path, tool)
    assert asyncio.run(wrapped.on_invoke_tool(None, '{}')) == 'original result'
    assert tool.on_invoke_tool is original


def test_no_active_revision_and_missing_binding(hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cards.store, 'active_row', lambda *a, **k: None)
    monkeypatch.setattr(cards, '_record_file', lambda *a: None)
    assert cards.active(hub) == (None, None)
    assert cards.card_for_tool(hub, 'unknown') is None
    assert cards.present(hub, 'unknown', '{}', '{}') is None


def test_retained_resource_must_belong_to_card(frame, monkeypatch):
    monkeypatch.setattr(cards.store, 'resource_row', lambda *a: {'card_id': 'different', 'html': 'secret'})
    assert cards.retained_html(frame.hub, 'custom', 'abc') is None
    monkeypatch.setattr(cards.store, 'resource_row', lambda *a: {'card_id': 'custom', 'html': 'safe'})
    assert cards.retained_html(frame.hub, 'custom', 'abc') == 'safe'


def test_custom_text_prefers_text_then_title_then_json():
    assert cards._custom_text({'text': 'Body', 'title': 'Title'}) == 'Body'
    assert cards._custom_text({'title': 'Title'}) == 'Title'
    assert cards._custom_text({'title': 1}) == json.dumps({'title': 1})
