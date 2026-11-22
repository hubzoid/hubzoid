"""Workflow catch-up, retries and shutdown with in-process engine stand-ins."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hubzoid.workflows import context, personal, runtime


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for name in ('_DBOS', '_INITED', '_LAUNCHED', '_QUEUE', '_MD_QUEUE', '_REGISTRY', '_HUB_DIR',
                 '_HUB_NAME', '_ENGINE', '_OWNER', '_APP_VERSION', '_IDENTITY_STEP'):
        monkeypatch.setattr(runtime, name, getattr(runtime, name))
    for name in ('_LLM_STEP', '_AGENT_STEP', '_JEV_STEP', '_HITL_CREATE_STEP', '_HITL_READ_STEP',
                 '_PUBLISH_STEP', '_EMAIL_STEP'):
        monkeypatch.setattr(context, name, None)
    monkeypatch.setattr(runtime, '_REGISTRY', {})
    monkeypatch.setattr(runtime, '_HUB_DIR', tmp_path)
    monkeypatch.setattr(runtime, '_HUB_NAME', 'desk')
    monkeypatch.setattr(runtime, '_OWNER', None)
    return tmp_path


def wf(name, schedule='0 * * * *', **kw):
    return SimpleNamespace(name=name, schedule=schedule, timezone=None, catch_up=True, schedulable=False, **kw)


@pytest.mark.parametrize('minutes,expected', [(30, []), (200, ['hourly']), (235, [])])
def test_catch_up_latest_slot_unless_next_is_close(isolated, monkeypatch, minutes, expected):
    monkeypatch.setattr(runtime, '_REGISTRY', {'hourly': wf('hourly'), 'manual': wf('manual', None)})
    started = []
    monkeypatch.setattr(runtime, 'start', lambda *a, **kw: started.append((a, kw)))
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert runtime.catch_up(since, since + timedelta(minutes=minutes)) == expected
    if expected:
        assert started == [(('hourly',), {'scheduled_at': '2026-01-01T03:00:00+00:00',
                                         'envelope': {'trigger': 'schedule', 'scope': 'team', 'caught_up': True}})]
    else:
        assert started == []


def test_broken_catch_up_does_not_stop_other_workflows(isolated, monkeypatch, caplog):
    monkeypatch.setattr(runtime, '_REGISTRY', {'bad': wf('bad', 'invalid'), 'good': wf('good')})
    started = []
    monkeypatch.setattr(runtime, 'start', lambda *a, **kw: started.append(a[0]))
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert runtime.catch_up(since, since + timedelta(hours=2, minutes=20)) == ['good']
    assert started == ['good'] and "could not catch up 'bad'" in caplog.text


def test_personal_dispatch_failure_is_reported(isolated, monkeypatch):
    from hubzoid import access

    scheduled = wf('daily', None)
    scheduled.schedulable = True
    monkeypatch.setattr(runtime, '_REGISTRY', {'daily': scheduled})
    monkeypatch.setattr(access, 'store_for', lambda *a: SimpleNamespace(schedule_hold=lambda: False,
                                                                      paused_workflows=lambda *a: []))

    def failed(*a):
        raise OSError('queue unavailable')

    monkeypatch.setattr(personal, 'fire_due', failed)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match='Could not dispatch workflows: personal schedules'):
        runtime.tick(last=now - timedelta(minutes=1), now=now)


@pytest.mark.parametrize('value,attempts,cap', [('bad', 1, None), (0, 1, 1), (3, 3, 3)])
def test_invalid_retry_setting_falls_back_to_single_attempt(monkeypatch, value, attempts, cap):
    monkeypatch.setattr(runtime, '_load_settings', lambda: {'agent_max_attempts': value,
                                                           'max_concurrent_workflows': value})
    assert runtime._agent_max_attempts() == attempts and runtime._hub_workflow_cap() == cap


def test_delivery_step_key_stays_stable_for_same_work_and_changes_for_new_content(isolated, monkeypatch):
    monkeypatch.setattr(runtime, '_DBOS', SimpleNamespace(workflow_id='w1', step_id=2))
    assert runtime._step_key() == 'w1:2'
    with context.run_scope(hub='desk', workflow='daily', hub_dir=isolated, engine=None,
                           work={'item': {'id': 'item-1', 'generation': 3}}):
        one = runtime._step_key({'a': 1, 'b': 2})
        assert one == runtime._step_key({'b': 2, 'a': 1})
        assert one.startswith('wi:item-1:g3:2:') and one != runtime._step_key({'a': 2, 'b': 2})
    runtime._DBOS.step_id = None
    assert runtime._step_key() is None


def test_shutdown_resets_state_even_when_engine_and_retirement_fail(isolated, monkeypatch, caplog):
    closed = []

    def failed(*a, **kw):
        raise OSError('teardown failed')

    owner = SimpleNamespace(retire=failed, close=lambda: closed.append(True))
    monkeypatch.setattr(runtime, '_INITED', True)
    monkeypatch.setattr(runtime, '_LAUNCHED', True)
    monkeypatch.setattr(runtime, '_OWNER', owner)
    monkeypatch.setattr(runtime, '_DBOS', SimpleNamespace(destroy=failed))
    monkeypatch.setattr(runtime.threading, 'enumerate', lambda: [])
    runtime.shutdown(completion_timeout_sec=0)
    assert runtime._INITED is False and runtime._LAUNCHED is False and runtime._DBOS is None
    assert runtime._OWNER is None and runtime._HUB_DIR is None and closed == [True]
    assert 'DBOS shutdown failed' in caplog.text and 'could not retire the owner lease' in caplog.text


def test_shutdown_of_old_owner_never_stops_successor(isolated, monkeypatch):
    monkeypatch.setattr(runtime, '_INITED', True)
    current = object()
    monkeypatch.setattr(runtime, '_OWNER', current)
    runtime.shutdown(only_for=object())
    assert runtime._INITED is True and runtime._OWNER is current


def test_init_refuses_second_hub_and_unsupported_sqlite(isolated, monkeypatch):
    monkeypatch.setattr(runtime, '_INITED', True)
    assert runtime.init(isolated) is None
    with pytest.raises(RuntimeError, match='Each hub needs its own workflow bridge process'):
        runtime.init(isolated / 'other')
    monkeypatch.setattr(runtime, '_INITED', False)
    monkeypatch.setattr(runtime.db, 'dbos_url', lambda *a: 'sqlite:///fake')
    monkeypatch.setattr(runtime, 'sqlite_problem', lambda *a: 'unsupported sqlite')
    with pytest.raises(RuntimeError, match='Unsupported sqlite'):
        runtime.init(isolated)
    assert runtime._INITED is False


def test_code_version_without_installed_distribution_still_tracks_files(isolated, monkeypatch):
    from importlib import metadata

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, 'version', missing)
    folder = isolated / 'workflows' / 'one'
    folder.mkdir(parents=True)
    source = folder / 'main.py'
    source.write_text('VALUE = 1')
    before = runtime._workflow_code_version(isolated, 'desk')
    cache = folder / '__pycache__'
    cache.mkdir()
    (cache / 'ignored.py').write_text('IGNORE = 1')
    assert runtime._workflow_code_version(isolated, 'desk') == before
    source.write_text('VALUE = 2')
    assert runtime._workflow_code_version(isolated, 'desk') != before


def test_unreadable_input_schema_falls_back_to_empty(isolated, monkeypatch):
    def broken(*a):
        raise OSError('database unavailable')

    monkeypatch.setattr(runtime.db, 'operational_engine', broken)
    assert runtime.input_schemas(isolated, 'desk') == {}
