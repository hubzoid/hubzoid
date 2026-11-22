"""Chat queue edits, workflow delivery and fail-closed credential checks."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from hubzoid.chat import engine as mod
from hubzoid.chat import graph as g
from tests.test_chat_link import chat  # noqa: F401


def queued(chat, text='first', **extra):
    _, engine, actor, link = chat
    outcome, plan = engine.enqueue(link['conversation_id'], actor, {
        'branch_id': link['branch_id'], 'content': text, 'lane': 'steer', **extra}, key=None)
    assert plan is None and outcome.status == 201 and outcome.body['dispatched'] is False
    return outcome.body['item']


def patch(chat, item, **body):
    _, engine, actor, link = chat
    return engine.queue_patch(link['conversation_id'], actor, item['id'], {
        'expected_revision': item['revision'], **body})


def test_queue_edit_reorders_between_neighbours_and_lanes(chat):  # noqa: F811
    one, two, three = queued(chat, 'one'), queued(chat, 'two'), queued(chat, 'three')
    edited = patch(chat, two, action='edit', content='corrected')
    assert edited['content'] == [{'type': 'text', 'text': 'corrected'}] and edited['revision'] == 2
    moved = patch(chat, three, action='move', before_id=two['id'])
    assert one['rank'] < moved['rank'] < two['rank']
    moved = patch(chat, moved, action='move', before_id=one['id'])
    assert moved['rank'] < one['rank']
    moved = patch(chat, moved, action='move', lane='queue')
    assert moved['lane'] == 'queue' and moved['rank'] == 1
    moved = patch(chat, moved, action='move')
    assert moved['rank'] == 1 and moved['revision'] == 5


@pytest.mark.parametrize('body,status,code', [
    ({'action': 'unknown'}, 400, 'invalid_operation'),
    ({'action': 'edit', 'content': 'new', 'expected_revision': 99}, 409, 'item_conflict'),
    ({'action': 'move', 'lane': 'outside'}, 400, 'invalid_operation'),
    ({'action': 'move', 'before_id': 'missing'}, 422, 'invalid_operation'),
    ({'action': 'rebase'}, 409, 'item_conflict'),
])
def test_bad_queue_edits_do_not_change_item(chat, body, status, code):  # noqa: F811
    item = queued(chat)
    with pytest.raises(mod.Err) as exc:
        patch(chat, item, **body)
    assert exc.value.status == status and exc.value.code == code
    _, engine, actor, link = chat
    assert engine.queue_list(link['conversation_id'], actor)[0]['revision'] == 1


def test_removed_item_cannot_be_edited_and_dependents_block(chat):  # noqa: F811
    one = queued(chat)
    two = queued(chat, 'two', predecessor_id=one['id'])
    three = queued(chat, 'three', predecessor_id=two['id'])
    removed = patch(chat, one, action='remove')
    assert removed['state'] == 'removed' and removed['reason'] == 'removed by its author'
    _, engine, actor, link = chat
    remaining = engine.queue_list(link['conversation_id'], actor)
    assert {r['id']: r['state'] for r in remaining} == {two['id']: 'blocked', three['id']: 'blocked'}
    with pytest.raises(mod.Err) as exc:
        patch(chat, removed, action='move')
    assert exc.value.code == 'item_conflict' and 'already removed' in exc.value.message
    with pytest.raises(mod.Err) as exc:
        patch(chat, {'id': 'missing', 'revision': 1}, action='remove')
    assert exc.value.code == 'not_found'


def test_rebase_acknowledges_current_branch_and_pause(chat):  # noqa: F811
    item = queued(chat)
    _, engine, actor, link = chat
    with engine.engine.begin() as conn:
        conn.execute(g.queue.update().where(g.queue.c.id == item['id']).values(state='needs_review'))
        conn.execute(g.branches.update().where(g.branches.c.id == link['branch_id']).values(queue_paused=1))
    rebased = patch(chat, item, action='rebase')
    assert rebased['state'] == 'paused' and rebased['reason'] is None
    with engine.engine.connect() as conn:
        row = conn.execute(sa.select(g.queue).where(g.queue.c.id == item['id'])).one()
        branch = g.branch_row(conn, link['branch_id'])
    frozen = json.loads(row.content_json)
    assert frozen['branch_revision'] == branch.revision and frozen['parent_id'] == branch.head_id


def test_queue_is_private_to_its_author(chat):  # noqa: F811
    item = queued(chat, 'private text')
    _, engine, actor, link = chat
    other = mod.Actor('other', 'other@example.org')
    with engine.engine.begin() as conn:
        conn.execute(g.members.insert().values(conversation_id=link['conversation_id'], principal_type='user',
                     principal_id='other', role='editor', added_by=actor.id, created_at=1))
    assert 'content' not in engine.queue_list(link['conversation_id'], other)[0]
    with pytest.raises(mod.Err) as exc:
        engine.queue_patch(link['conversation_id'], other, item['id'], {'action': 'remove', 'expected_revision': 1})
    assert exc.value.code == 'not_found'


def test_workflow_delivery_is_private_and_idempotent(chat):  # noqa: F811
    _, engine, actor, link = chat
    notices = []
    engine.on_terminal = lambda **kw: notices.append(kw)
    cid = engine.deliver(actor, agent='Test', title='Daily summary', prompt='Summarize', answer='Seven items',
                         conversation_id=link['conversation_id'])
    assert cid == link['conversation_id']
    assert engine.deliver(actor, agent='Test', title='Ignored', prompt='Ignored', answer='Ignored',
                           conversation_id=cid) == cid
    snap = engine.snapshot(cid, actor)
    assert snap['conversation']['title'] == 'Daily summary' and snap['conversation']['title_source'] == 'manual'
    assert [(m['role'], m['content']) for m in snap['messages']] == [
        ('user', [{'type': 'text', 'text': 'Summarize'}]),
        ('assistant', [{'type': 'text', 'text': 'Seven items'}])]
    assert len(notices) == 1 and notices[0]['status'] == 'complete'
    with pytest.raises(mod.Err) as exc:
        engine.readable(cid, mod.Actor('other', 'other@example.org'))
    assert exc.value.code == 'not_found'


def test_workflow_delivery_can_create_conversation(chat):  # noqa: F811
    _, engine, actor, _ = chat
    engine.on_terminal = lambda **kw: None
    cid = engine.deliver(actor, agent='Test', title='Report', prompt='Count', answer='3')
    assert engine.snapshot(cid, actor)['conversation']['title'] == 'Report'
    assert len(engine.snapshot(cid, actor)['messages']) == 2


def test_credentials_keep_hub_scope_and_fail_closed(chat, monkeypatch):  # noqa: F811
    from hubzoid.access import evaluator
    from hubzoid.auth import users

    _, engine, actor, _ = chat
    monkeypatch.setattr(users, 'epoch', lambda *a: 7)
    keyed = replace(actor, credential='api_key', credential_id='k1', key_hubs=('desk',))
    credential = engine._credential_of(keyed)
    assert credential == {'kind': 'api_key', 'id': 'k1', 'hubs': ['desk'], 'epoch': 7}
    assert engine._with_credential(actor, credential) == keyed
    assert engine._with_credential(actor, None) is actor
    assert engine._credential_of(replace(actor, credential='')) is None
    assert engine._credential_valid(actor.id, None) is True

    def broken(*a):
        raise OSError('store down')

    monkeypatch.setattr(users, 'epoch', broken)
    assert engine._credential_of(actor)['epoch'] is None
    monkeypatch.setattr(evaluator, 'still_valid', broken)
    assert engine._credential_valid(actor.id, credential) is False
    monkeypatch.setattr(users, 'get', lambda *a: {'email': 'new@example.org'})
    assert engine._author({'owner_id': actor.id}, 'different').email == 'new@example.org'


@pytest.mark.parametrize('failure,wanted', [(ProcessLookupError, True), (PermissionError, False), (None, False)])
def test_dead_owner_probe_uses_only_same_host_other_process(monkeypatch, failure, wanted):
    monkeypatch.setattr(mod.socket, 'gethostname', lambda: 'testhost')
    monkeypatch.setattr(mod.os, 'getpid', lambda: 700)
    seen = []

    def probe(pid, signal):
        seen.append((pid, signal))
        if failure:
            raise failure('unavailable')

    monkeypatch.setattr(mod.os, 'kill', probe)
    assert mod._owner_gone('testhost:701:nonce') is wanted
    assert seen == [(701, 0)]
    assert mod._owner_gone('remotehost:701:x') is False
    assert mod._owner_gone('testhost:700:x') is False


@pytest.mark.parametrize('content,status,message', [
    ([], 422, 'non-empty list'), ([{'type': 'file'}], 422, 'Each part here'),
    ('', 422, 'Write a message'), ('x' * 1_000_001, 413, 'too long'),
])
def test_plain_message_validation(content, status, message):
    with pytest.raises(mod.Err) as exc:
        mod._text_parts(content)
    assert exc.value.status == status and message in exc.value.message


def test_default_view_recovers_leaf_when_branch_has_no_head():
    actor = mod.Actor('a', 'a@example.org')
    branches = [{'id': 'b1', 'writer_actor_id': 'a', 'updated_at': 2, 'head_id': None}]
    msgs = [{'id': 'm1', 'parent_id': None}, {'id': 'm2', 'parent_id': 'm1'}]
    assert mod.ChatEngine._default_view(branches, msgs, actor, {'revision': 9}) == {
        'branch_id': 'b1', 'view_head_id': 'm2', 'revision': 9}
    assert mod.ChatEngine.message_json({'id': 'm1', 'status': 'error', 'usage': {
        'input_tokens': 2, 'secret': 'hidden', 'error_kind': 'quota'}})['error_kind'] == 'quota'
