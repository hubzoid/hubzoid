"""In-process checkpoint seams for workflow model calls and human decisions."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from hubzoid import access, db
from hubzoid.workflows import context, identity, runtime
from tests.test_cov_workflows_context import hub  # noqa: F401


class Answer(BaseModel):
    total: int


class Inputs(BaseModel):
    region: str


@pytest.fixture
def scoped(hub, monkeypatch):  # noqa: F811
    for name in ('_LLM', '_AGENT', '_JEV', '_LLM_STEP', '_AGENT_STEP', '_JEV_STEP',
                 '_EMAIL_STEP', '_PUBLISH_STEP', '_HITL_CREATE_STEP', '_HITL_READ_STEP'):
        monkeypatch.setattr(context, name, None)
    monkeypatch.setattr(runtime, '_OWNER', None)
    monkeypatch.setattr(identity, 'recheck', lambda *a, **k: None)
    with context.run_scope(hub='desk', workflow='daily', hub_dir=hub, engine=db.operational_engine(hub),
                           subject='workflow:daily', run_id='run:one', inputs=Inputs(region='eu')):
        yield context.hub


def test_checkpointed_model_calls_validate_results(scoped, monkeypatch):
    seen = []
    monkeypatch.setattr(context, '_LLM', lambda *a, **kw: pytest.fail('checkpoint must handle this call'))
    monkeypatch.setattr(context, '_LLM_STEP', lambda *a: seen.append(a) or {
        'text': '{"total":3}', 'json': {'total': 3}})
    assert scoped.call_llm('Count', response_model=Answer).total == 3
    assert seen[0][0]['response_format'] == 'json'
    assert seen[0][0]['schema']['required'] == ['total'] and seen[0][2] == 'workflow:daily'
    with pytest.raises(ValueError, match='response_format'):
        scoped.call_llm('Count', response_format='yaml')


def test_checkpointed_agent_and_jev(scoped, monkeypatch):
    monkeypatch.setattr(context, '_AGENT', lambda *a, **kw: pytest.fail('checkpoint must handle this call'))
    monkeypatch.setattr(context, '_AGENT_STEP', lambda *a: 'complete')
    assert scoped.call_agent('Do work') == 'complete'
    monkeypatch.setattr(context, '_JEV', lambda *a, **kw: pytest.fail('checkpoint must handle this call'))
    seen = []
    monkeypatch.setattr(context, '_JEV_STEP', lambda *a: seen.append(a) or {'answers': {'ready': True}})
    assert scoped.call_jev('state', {'ready': {'type': 'noul'}}) == {'ready': True}
    assert seen[0][0]['state'] == 'state' and seen[0][2] == 'workflow:daily'


@pytest.mark.parametrize('name,args', [('call_agent', ('task',)), ('call_jev', ('state', {}))])
def test_unconfigured_seams_name_the_boot_action(scoped, name, args):
    with pytest.raises(RuntimeError, match='workflows.configure'):
        getattr(scoped, name)(*args)


def test_station_requires_stage_and_forwards_bounded_call(scoped, monkeypatch):
    from hubzoid.stations import service

    with pytest.raises(ValueError, match='needs stage='):
        scoped.call_agent('Do it', agent='reviewer')
    seen = []
    monkeypatch.setattr(service, 'call', lambda *a, **kw: seen.append((a, kw)) or {'ok': True})
    assert scoped.call_agent('Review', agent='reviewer', stage='review', revision=2,
                             checkout={'url': 'https://example.org/repo'}) == {'ok': True}
    assert seen == [(('reviewer', 'Review'), {'stage': 'review', 'revision': 2, 'response_model': None,
                     'timeout': None, 'checkout': {'url': 'https://example.org/repo'}})]


def test_station_retry_and_handoffs(scoped, monkeypatch):
    from hubzoid.stations import service

    monkeypatch.setattr(service, 'retry', lambda call, clarification, **kw: {
        'call': call, 'clarification': clarification})
    monkeypatch.setattr(service, 'save_handoff_here', lambda kind, title, body: {
        'id': 'h1', 'kind': kind, 'title': title, 'body': body})
    monkeypatch.setattr(service, 'read_handoff_here', lambda hid: {'id': hid, 'body': 'saved'})
    assert scoped.retry_station('c1', 'Use approved sources') == {'call': 'c1', 'clarification': 'Use approved sources'}
    assert scoped.save_handoff('plan', 'Plan', 'Steps')['body'] == 'Steps'
    assert scoped.read_handoff('h1') == {'id': 'h1', 'body': 'saved'}


def test_publish_tracks_series_url_and_unchanged_copy(scoped, monkeypatch):
    seen = []
    monkeypatch.setattr(context, '_PUBLISH_STEP', lambda *a: seen.append(a) or {
        'id': 'a1', 'title': 'Daily', 'url': '/copy', 'share_url': '/shared', 'series_id': 's1', 'unchanged': True})
    out = scoped.publish_artifact('output/report.html', key='daily', audience='people',
                                   share_with=[{'email': 'a@example.org'}])
    assert out['id'] == 'a1' and context._ctx().published == [
        {'key': 'daily', 'title': 'Daily', 'url': '/shared', 'series_id': 's1', 'changed': False}]
    assert seen[0][-1]['series'] == {'scope': 'team', 'schedule_id': None, 'inputs': {'region': 'eu'}}
    assert seen[0][-1]['share_with'] == [{'email': 'a@example.org'}]


def test_email_formats_and_failure_policy(scoped, monkeypatch):
    from hubzoid.email_delivery import EmailError

    with pytest.raises(ValueError, match='body_format'):
        scoped.send_email('Subject', body_format='xml')
    seen = []
    monkeypatch.setattr(context, '_EMAIL_STEP', lambda *a: seen.append(a) or {
        'status': 'refused', 'sent': False, 'message': 'No recipient'})
    out = scoped.send_email('Subject', 'Body', artifacts=[{'id': 'a1'}, 'a2'], email_list='leads',
                             body_format='markdown', raise_on_failure=False)
    assert out['status'] == 'refused'
    assert seen[0][-1] == {'subject': 'Subject', 'body': 'Body', 'artifacts': ['a1', 'a2'],
                           'email_list': 'leads', 'body_format': 'markdown'}
    with pytest.raises(EmailError, match='No recipient'):
        scoped.send_email('Subject')


def test_personal_run_never_sends_to_a_list(scoped, monkeypatch):
    from hubzoid.email_delivery import EmailError

    context._ctx().run['scope'] = 'personal'
    monkeypatch.setattr(context, '_EMAIL_STEP', lambda *a: pytest.fail('personal list mail must not send'))
    out = scoped.send_email('Subject', email_list='leads', raise_on_failure=False)
    assert out['status'] == 'refused' and out['sent'] is False and out['recipient'] is None
    with pytest.raises(EmailError, match='Email lists are for team runs'):
        scoped.send_email('Subject', email_list='leads')


def test_approve_and_question_return_decision(scoped, monkeypatch):
    seen = []
    monkeypatch.setattr(context, '_HITL_CREATE_STEP', lambda spec: seen.append(spec) or {
        'status': 'approved', 'note': 'Proceed', 'decided_by_email': 'a@example.org', 'answer': 'North'})
    monkeypatch.setattr(context, '_wait_out_hold', lambda *a: None)
    assert scoped.approve('Send', details={'count': 2}) == {
        'approved': True, 'note': 'Proceed', 'by': 'a@example.org'}
    assert scoped.ask('Which region?', options=['North', {'label': 'South'}]) == 'North'
    assert seen[0]['arguments'] == {'count': 2} and seen[0]['approvers'] == ['requester']
    assert seen[1]['options'] == [{'label': 'North'}, {'label': 'South'}]


def test_pending_decision_posts_once_then_rechecks(scoped, monkeypatch):
    from hubzoid.workflows import chat_link

    pending = {'id': 'r1', 'status': 'pending'}
    sent, posted, read, waited = [], [], [], []
    monkeypatch.setattr(context, '_HITL_CREATE_STEP', lambda spec: pending)
    monkeypatch.setattr(context, '_HITL_READ_STEP', lambda *a: read.append(a) or {
        'id': 'r1', 'status': 'approved', 'note': None, 'decided_by_email': 'a@example.org'})
    monkeypatch.setattr(context, '_hitl_receive', lambda rid: True)
    monkeypatch.setattr(context, '_wait_out_hold', lambda *a: waited.append(a))
    monkeypatch.setattr(chat_link, 'post_request', lambda *a: posted.append(a))
    monkeypatch.setattr(context, '_EMAIL_STEP', lambda *a: sent.append(a) or {'status': 'previewed'})
    assert scoped.approve('Send')['approved'] is True
    assert len(posted) == len(sent) == len(read) == 1
    assert 'Waiting for you: Send' == sent[0][-1]['subject']
    assert read[0][-1] == 'r1' and waited[0][-1] == 'run:one'


def test_review_plan_publishes_exact_snapshot(scoped, monkeypatch):
    source = context._ctx().hub_dir / 'plan.md'
    source.write_text('Version one')
    published, asked = [], []

    def publish(path, **kw):
        published.append((path, path.read_bytes(), kw))
        source.write_text('Changed meanwhile')
        return {'url': '/plan-copy'}

    monkeypatch.setattr(context.hub, 'publish_artifact', publish)
    monkeypatch.setattr(context.hub, '_wait_for', lambda **kw: asked.append(kw) or {
        'status': 'approved', 'note': 'Looks good', 'decided_by_email': 'a@example.org'})
    out = scoped.review_plan(source, title='Launch plan', approvers=['a@example.org', 'requester'])
    assert out == {'approved': True, 'note': 'Looks good', 'by': 'a@example.org'}
    assert published[0][1] == b'Version one' and published[0][0].name == 'launch-plan.md'
    assert published[0][2] == {'title': 'Launch plan', 'audience': 'people', 'share_with': ['a@example.org']}
    assert asked[0]['subject']['digest'] == 'sha256:' + hashlib.sha256(b'Version one').hexdigest()
    assert asked[0]['subject']['url'] == '/plan-copy'


def test_hitl_replay_reuses_pending_request_and_versions_plan(tmp_path, monkeypatch):
    from hubzoid.hitl import service

    payload = {'hub_dir': str(tmp_path), 'workflow_id': 'wf-1', 'kind': 'approval', 'title': 'Plan',
               'requester': 'a@example.org', 'account_id': 'account-1', 'approvers': ['requester'],
               'subject': {'plan': True, 'digest': 'sha256:one'}}
    monkeypatch.setattr(service, 'pending_for_workflow', lambda *a: {'id': 'existing', 'status': 'pending'})
    assert context._hitl_create(payload) == {'id': 'existing', 'status': 'pending'}
    monkeypatch.setattr(service, 'pending_for_workflow', lambda *a: None)
    monkeypatch.setattr(service, 'count_for_workflow', lambda *a: 2)
    seen = []
    monkeypatch.setattr(service, 'create', lambda *a, **kw: seen.append((a, kw)) or {'id': 'new'})
    assert context._hitl_create(payload) == {'id': 'new'}
    assert seen[0][1]['subject']['version'] == 3 and seen[0][1]['workflow_id'] == 'wf-1'
    assert seen[0][0][2].email == 'a@example.org'
    monkeypatch.setattr(service, 'get', lambda *a: {'id': a[-1], 'status': 'approved'})
    assert context._hitl_read(str(tmp_path), 'ignored', 'new') == {'id': 'new', 'status': 'approved'}


def test_unreadable_backup_hold_is_bounded_and_cleanup_attempted(tmp_path, monkeypatch, caplog):
    import time

    now, sleeps, stores = [1.0], [], []

    def unavailable(*args):
        stores.append(args)
        raise OSError('locked')

    def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(access, 'store_for', unavailable)
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(time, 'sleep', advance)
    monkeypatch.setattr(context, 'HOLD_WAIT_LIMIT', 1)
    context._wait_out_hold(tmp_path, 'wf-1')
    assert sleeps == [5.0] and len(stores) == 3
    assert 'could not be read' in caplog.text and 'could not clear hold-waiting:wf-1' in caplog.text


def test_secret_reference_failure_uses_default(scoped, monkeypatch):
    from hubzoid import secretstore
    from hubzoid.hubs import settings

    monkeypatch.setattr(settings, 'secret_ref', lambda *a: 'secret:mail')

    def missing(*a):
        raise secretstore.SecretError('missing', 'No mail secret')

    monkeypatch.setattr(secretstore, 'resolve', missing)
    assert scoped.secret('mail', 'fallback') == 'fallback'
