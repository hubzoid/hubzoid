"""Portal workflow controls, audit boundaries and optional-service failures."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hubzoid import audit_log, portal
from hubzoid.workflows import access as wf_access
from hubzoid.workflows import client as wf_client
from hubzoid.workflows import control, observe, runtime
from tests.test_portal_api import client  # noqa: F401


def fail(*args, **kw):
    raise RuntimeError('private connection detail')


@pytest.fixture
def workflow_api(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(wf_access, 'canonical', lambda *a: 'daily')
    monkeypatch.setattr(wf_access, 'authorize', lambda *a: SimpleNamespace(allowed=True))
    monkeypatch.setattr(control, '_engine_here', lambda *a: False)
    monkeypatch.setattr(observe, 'runs', lambda *a, **kw: [])
    monkeypatch.setattr(runtime, 'input_schemas', lambda *a: {'daily': {'type': 'object'}})
    return client


def start(client, **kw):
    return client.post('/portal/api/workflows/run', json={'hub': client.hub, 'workflow': 'daily', **kw})


def test_workflow_run_permission_is_checked_before_engine(workflow_api, monkeypatch):
    monkeypatch.setattr(wf_access, 'authorize', lambda *a: SimpleNamespace(allowed=False))
    monkeypatch.setattr(control, '_engine_here', fail)
    out = start(workflow_api)
    assert out.status_code == 403 and "You can't run daily" in out.json()['detail']


def test_local_workflow_control_and_error_translation(workflow_api, monkeypatch):
    monkeypatch.setattr(control, '_engine_here', lambda *a: True)
    sent = []
    monkeypatch.setattr(control, 'start_now', lambda *a, **kw: sent.append(kw) or {
        'run_id': 'local-1', 'already_running': False})
    out = start(workflow_api, inputs={'region': 'eu'})
    assert out.status_code == 200 and out.json() == {'run_id': 'local-1', 'already_running': False}
    assert sent == [{'actor': 'root', 'surface': 'console', 'prefer': 'code', 'inputs': {'region': 'eu'}}]

    def held(*a, **kw):
        raise control.ControlError('Backups in progress', code='held')

    monkeypatch.setattr(control, 'start_now', held)
    out = start(workflow_api)
    assert out.status_code == 409 and out.json() == {'detail': 'Backups in progress'}


def test_remote_workflow_hold_and_running_run(workflow_api, monkeypatch):
    monkeypatch.setattr(type(workflow_api.gs), 'schedule_hold', lambda self: True)
    out = start(workflow_api)
    assert out.status_code == 409 and 'backup' in out.json()['detail']
    monkeypatch.setattr(type(workflow_api.gs), 'schedule_hold', lambda self: False)
    monkeypatch.setattr(observe, 'runs', lambda *a, **kw: [{'id': 'running-1'}])
    monkeypatch.setattr(wf_client, 'enqueue_live', fail)
    out = start(workflow_api)
    assert out.status_code == 200 and out.json() == {'run_id': 'running-1', 'already_running': True}


@pytest.mark.slow
def test_remote_workflow_envelope_and_missing_engine(workflow_api, monkeypatch):
    monkeypatch.setattr(wf_client, 'enqueue_live', lambda *a, **kw: None)
    out = start(workflow_api)
    assert out.status_code == 409 and "engine for this agent isn't running" in out.json()['detail']
    sent, closed = [], []
    handle = SimpleNamespace(get_workflow_id=lambda: 'remote-1',
                             client=SimpleNamespace(destroy=lambda: closed.append(True)))
    monkeypatch.setattr(wf_client, 'enqueue_live', lambda *a, **kw: sent.append((a, kw)) or handle)
    out = start(workflow_api, inputs={'region': 'eu'})
    assert out.status_code == 200 and out.json() == {'run_id': 'remote-1', 'already_running': False}
    envelope = sent[0][1]['envelope']
    assert envelope['trigger'] == 'operator' and envelope['scope'] == 'team'
    assert envelope['started_by'] == 'root' and envelope['inputs'] == {'region': 'eu'}
    assert closed == [True]


def test_workflow_catalog_attaches_input_schema(workflow_api, monkeypatch):
    monkeypatch.setattr(observe, 'catalog', lambda *a: [{'name': 'daily', 'kind': 'code'}])
    out = workflow_api.get('/portal/api/workflows', params={'hub': workflow_api.hub})
    assert out.status_code == 200 and out.json()['workflows'] == [
        {'name': 'daily', 'kind': 'code', 'input_schema': {'type': 'object'}}]
    out = workflow_api.get('/portal/api/workflows', params={'hub': 'unknown'})
    assert out.status_code == 404 and 'not registered' in out.json()['detail']


@pytest.mark.parametrize('missing', [True, False])
def test_schedule_runs_must_belong_to_selected_hub(client, monkeypatch, missing):  # noqa: F811
    from hubzoid.workflows import personal

    monkeypatch.setattr(personal, 'get', lambda *a: None if missing else SimpleNamespace(hub='other'))
    out = client.get('/portal/api/schedules/s1/runs', params={'hub': client.hub})
    assert out.status_code == 404 and out.json() == {'detail': 'No such schedule on this agent.'}


def test_schedule_cost_and_latest_runs_projection(client, monkeypatch):  # noqa: F811
    from hubzoid import usage
    from hubzoid.workflows import personal

    schedule = SimpleNamespace(id='s1', hub=client.hub, workflow='daily', owner='owner@example.org')
    monkeypatch.setattr(personal, 'for_hub', lambda *a: [schedule])
    monkeypatch.setattr(personal, 'row', lambda *a: {'id': 's1', 'title': 'Daily'})
    monkeypatch.setattr(personal, 'get', lambda *a: schedule)
    monkeypatch.setattr(runtime, 'input_schemas', lambda *a: {})
    monkeypatch.setattr(usage, 'run_cost', lambda *a: 0.123456)
    monkeypatch.setattr(observe, 'schedule_runs', lambda *a: [{'id': 'run-1', 'state': 'SUCCESS'}])
    out = client.get('/portal/api/schedules', params={'hub': client.hub})
    assert out.status_code == 200 and out.json() == {'schedules': [
        {'id': 's1', 'title': 'Daily', 'owner': 'owner@example.org', 'cost_30d': 0.1235}]}
    out = client.get('/portal/api/schedules/s1/runs', params={'hub': client.hub})
    assert out.status_code == 200 and out.json()['runs'] == [{'id': 'run-1', 'state': 'SUCCESS'}]


def test_run_history_failure_is_actionable_and_scrubbed(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(observe, 'runs', fail)
    out = client.get('/portal/api/runs', params={'hub': client.hub})
    assert out.status_code == 503 and out.json() == {
        'detail': 'Run history unavailable; check the workflow database and server logs.'}
    assert 'private connection' not in out.text


def test_audit_auth_requires_org_admin(client):  # noqa: F811
    client.admin['who'] = portal.PortalAdmin('manager', False, [client.hub])
    out = client.get('/portal/api/audit?kind=auth')
    assert out.status_code == 403 and 'organization administrator' in out.json()['detail']


def test_audit_event_projection_and_invalid_filter(client, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(audit_log, 'read_events', lambda *a, **kw: seen.append(kw) or [
        {'kind': 'tool', 'subject': 'alice', 'ts': '2026-11-22T10:00:00.123Z', 'tool': 'read'}])
    out = client.get('/portal/api/audit?kind=tool&outcome=deny&result=error')
    assert out.status_code == 200
    assert out.json()['rows'] == [{'kind': 'tool', 'hub': client.hub, 'user': 'alice',
                                   'ts': '2026-11-22T10:00:00+00:00', 'tool': 'read'}]
    assert seen[0]['decision'] == 'deny' and seen[0]['outcome'] == 'error'

    def invalid(*args, **kw):
        raise ValueError('since must be an ISO date')

    monkeypatch.setattr(audit_log, 'read_events', invalid)
    out = client.get('/portal/api/audit?kind=tool')
    assert out.status_code == 422 and out.json()['detail'] == 'since must be an ISO date'


@pytest.mark.parametrize('kind', ['', 'unknown', 'tool,unknown'])
@pytest.mark.slow
def test_audit_export_names_invalid_kind(client, kind):  # noqa: F811
    out = client.get('/portal/api/audit/export', params={'kind': kind})
    assert out.status_code == 422 and out.json()['detail'].startswith('kind must be some of')


def test_audit_export_streams_records_and_scopes_delegates(client, monkeypatch):  # noqa: F811
    seen = []
    rows = [{'kind': 'tool', 'subject': 'alice'}, {'kind': 'tool', 'subject': 'bob'}]
    monkeypatch.setattr(audit_log, 'export', lambda *a, **kw: seen.append(kw) or iter(rows))
    client.admin['who'] = portal.PortalAdmin('manager', False, [client.hub])
    out = client.get('/portal/api/audit/export?kind=tool,admin,auth')
    assert out.status_code == 200 and [json.loads(line) for line in out.text.splitlines()] == rows
    assert out.headers['cache-control'] == 'no-store'
    assert seen[0]['kinds'] == ('tool',) and seen[0]['hubs'] == [client.hub]
    out = client.get('/portal/api/audit/export?kind=admin,auth')
    assert out.status_code == 403 and 'organization administrator' in out.json()['detail']
    client.admin['who'] = portal.PortalAdmin('manager', False, [])
    out = client.get('/portal/api/audit/export?kind=tool')
    assert out.status_code == 404 and out.json()['detail'] == 'No agent here that you manage.'


def test_audit_export_rejects_invalid_window(client, monkeypatch):  # noqa: F811
    def invalid(*a, **kw):
        raise ValueError('until precedes since')

    monkeypatch.setattr(audit_log, 'export', invalid)
    out = client.get('/portal/api/audit/export')
    assert out.status_code == 422 and out.json()['detail'] == 'until precedes since'


def test_optional_tabs_survive_failed_backends(tmp_path, monkeypatch):
    from hubzoid.intake import service as intake
    from hubzoid.sandbox import config as sb_config
    from hubzoid.work_items import store as wi_store

    monkeypatch.setattr(wi_store, 'list_items', fail)
    monkeypatch.setattr(intake, 'list_items', fail)
    monkeypatch.setattr(sb_config, 'list_profiles', fail)
    assert portal._present(tmp_path, 'desk') == {'work': False, 'intake': False, 'sandboxes': False}


def test_connection_tab_available_or_hidden_on_error(tmp_path, monkeypatch):
    from hubzoid.connectors import per_user, registry, tokens

    monkeypatch.setattr(registry, 'list_all', lambda *a: [
        SimpleNamespace(id='mail', enabled=True, auth_type='oauth'),
        SimpleNamespace(id='shared', enabled=True, auth_type='shared')])
    seen = []
    monkeypatch.setattr(per_user, 'allowed_ids', lambda h, e, ids: seen.append(ids) or [])
    monkeypatch.setattr(tokens, 'for_user', lambda *a: [{'connector_id': 'mail'}])
    assert portal._has_connections(tmp_path, 'account', 'email') is True and seen == [['mail']]
    monkeypatch.setattr(registry, 'list_all', fail)
    assert portal._has_connections(tmp_path, 'account', 'email') is False


def test_company_link_notice_is_best_effort(tmp_path, monkeypatch, caplog):
    from hubzoid import email_delivery
    from hubzoid.artifacts import domains
    from hubzoid.workflows import personal

    found = SimpleNamespace(hub='desk', owner='alice@example.org', title='Daily', key='daily',
                            workflow='daily', id='s1', domain_changed=7)
    sent = []
    monkeypatch.setattr(personal, 'email_on', lambda *a: True)
    monkeypatch.setattr(domains, 'label', lambda: 'Company')
    monkeypatch.setattr(email_delivery, 'send_to_owner', lambda *a, **kw: sent.append(kw))
    portal._tell_owner_link_off(tmp_path, found)
    assert sent[0]['subject'] == 'Daily: company link turned off'
    assert sent[0]['idem_key'] == 'series:s1:off:7' and sent[0]['identity'].subject == 'alice@example.org'
    monkeypatch.setattr(email_delivery, 'send_to_owner', fail)
    portal._tell_owner_link_off(tmp_path, found)
    assert 'could not tell alice@example.org' in caplog.text
