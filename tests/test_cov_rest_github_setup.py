"""GitHub setup state, repository resolution and token rollback failures."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hubzoid.channels.errors import ChannelError
from hubzoid.channels.github import setup
from tests.test_channels_github import hub_dir  # noqa: F401


@pytest.fixture
def conn(hub_dir):  # noqa: F811
    return SimpleNamespace(hub_dir=hub_dir, hub=hub_dir.name, id='github', provider='github', revision=1, state={},
        config=SimpleNamespace(workflow='factory'),
        settings=SimpleNamespace(mode='app', app_id=1, installation_id=2,
            owner=SimpleNamespace(type='organization', login='example', id=7), repositories=[],
            rules=SimpleNamespace(status_failures=False)),
        adapter=SimpleNamespace(bot_login=lambda c: 'hubzoid'))


@pytest.mark.parametrize('status,expiry,code', [('converting', 200, 'setup_conversion_unknown'),
    ('ready', 200, 'setup_state_invalid'), ('awaiting_registration', 99, 'setup_expired')])
def test_setup_state_and_expiry_are_authoritative(conn, monkeypatch, status, expiry, code):
    monkeypatch.setattr(setup.time, 'time', lambda: 100)
    conn.state['github_setup'] = {'state_hash': setup._hash('state'), 'status': status, 'expires': expiry}
    with pytest.raises(ChannelError) as exc:
        setup._setup(conn, 'state', want=('awaiting_registration',))
    assert exc.value.code == code


def test_setup_requires_public_url_and_github_connection(conn, monkeypatch):
    monkeypatch.delenv('HUBZOID_PUBLIC_URL', raising=False)
    with pytest.raises(ChannelError) as exc:
        setup.manifest(conn, 'App')
    assert exc.value.code == 'public_url_required'
    conn.provider = 'linear'
    with pytest.raises(ChannelError) as exc:
        setup.start(conn, owner='example', owner_type='user', name='App', actor='owner')
    assert exc.value.code == 'not_found'
    conn.provider = 'github'
    with pytest.raises(ChannelError) as exc:
        setup.start(conn, owner='example', owner_type='unknown', name='App', actor='owner')
    assert exc.value.code == 'validation_failed'


@pytest.mark.parametrize('failure,code', [(ChannelError, 'reauth_required'), (OSError, 'provider_unavailable')])
def test_repository_listing_preserves_refusals_and_masks_unexpected_errors(conn, monkeypatch, failure, code):
    def broken(*a):
        if failure is ChannelError:
            raise ChannelError('reauth_required', 'Reconnect', status=503)
        raise OSError('private connection string')

    monkeypatch.setattr(setup.gh, 'installation_client', broken)
    with pytest.raises(ChannelError) as exc:
        setup.repositories(conn)
    assert exc.value.code == code and 'private connection' not in exc.value.message


def test_pat_repository_list_and_immutable_name_resolution(conn, monkeypatch):
    conn.settings.mode = 'pat'
    web = SimpleNamespace(request=lambda *a, **kw: SimpleNamespace(json=lambda: [
        {'id': 9, 'full_name': 'Example/Widget', 'private': True}]))
    monkeypatch.setattr(setup.gh, 'for_repo', lambda *a: web)
    assert setup.repositories(conn) == [{'id': 9, 'full_name': 'Example/Widget', 'private': True}]
    assert setup.resolve_names(conn, ['example/widget']) == [{'id': 9, 'full_name': 'Example/Widget'}]
    with pytest.raises(ChannelError) as exc:
        setup.resolve_names(conn, ['example/missing'])
    assert exc.value.code == 'repository_not_installed' and exc.value.status == 409


def test_cancel_removes_setup_and_audits_actor(conn, monkeypatch):
    seen, audited = [], []
    monkeypatch.setattr(setup.store, 'set_connection_state', lambda *a, **kw: seen.append(kw))
    monkeypatch.setattr(setup.config_lib, 'audit', lambda *a: audited.append(a))
    setup.cancel(conn, 'owner')
    assert seen == [{'github_setup': {}}] and audited[0][1:] == ('owner', 'github_setup_cancel', 'github')


@pytest.mark.parametrize('phase,code', [('user', 'credential_rejected'), ('repo', 'repository_not_installed')])
def test_pat_credentials_checked_before_storing(conn, monkeypatch, phase, code):
    def request(method, path):
        if phase == 'user' or path.startswith('/repos/'):
            raise RuntimeError('refused')
        return SimpleNamespace(json=lambda: {'login': 'owner', 'id': 7})

    monkeypatch.setattr(setup.gh, 'pat_client_raw', lambda token: SimpleNamespace(request=request))
    with pytest.raises(ChannelError) as exc:
        setup.connect_pat(conn.hub_dir, conn.id, token='test-token', repositories_=['example/widget'],
                           actor='owner', expected_revision='absent')
    assert exc.value.code == code


@pytest.mark.parametrize('old,restore_fails', [(None, False), ('previous-token', False), ('previous-token', True)])
def test_refused_pat_connect_restores_previous_secret(conn, monkeypatch, old, restore_fails, caplog):
    from hubzoid.secretstore import store as secrets

    web = SimpleNamespace(request=lambda *a, **kw: SimpleNamespace(json=lambda: {'login': 'owner', 'id': 7}))
    monkeypatch.setattr(setup.gh, 'pat_client_raw', lambda token: web)
    monkeypatch.setattr(setup.config_lib, 'export', lambda *a: {'revision': 1})
    monkeypatch.setattr(setup.config_lib, 'applied_base', lambda *a: {'provider_config': {}})
    monkeypatch.setattr(secrets, 'value', lambda *a, **kw: old)
    writes, deletes = [], []

    def put(*a, **kw):
        writes.append(kw['value'])
        if len(writes) > 1 and restore_fails:
            raise OSError('disk unavailable')

    def refused(*a, **kw):
        raise ChannelError('revision_conflict', 'Configuration changed', status=409)

    monkeypatch.setattr(secrets, 'put', put)
    monkeypatch.setattr(secrets, 'delete', lambda *a, **kw: deletes.append(kw['name']))
    monkeypatch.setattr(setup.config_lib, 'apply', refused)
    with pytest.raises(ChannelError) as exc:
        setup.connect_pat(conn.hub_dir, conn.id, token='new-token', repositories_=[],
                           actor='owner', expected_revision=1)
    assert exc.value.code == 'revision_conflict'
    assert writes == (['new-token', old] if old else ['new-token'])
    assert deletes == ([] if old else ['token'])
    if restore_fails:
        assert 'could not restore the token' in caplog.text


@pytest.mark.parametrize('mode,url,code', [('app', 'https://github.com/example/widget/issues/1', 'pat_only'),
    ('pat', 'https://evil.example/example/widget/issues/1', 'validation_failed'),
    ('pat', 'https://github.com/example/widget/issues/1', 'repository_not_configured')])
def test_manual_intake_refuses_before_provider_access(conn, mode, url, code):
    conn.settings.mode = mode
    with pytest.raises(ChannelError) as exc:
        setup.manual_intake(conn, url=url, task='Review')
    assert exc.value.code == code


def test_import_app_preserves_owner_and_continues_verified_installation(conn, monkeypatch):
    states, verified = [], []
    monkeypatch.setattr(setup.store, 'set_connection_state', lambda *a, **kw: states.append(kw['github_setup']))
    monkeypatch.setattr(setup.config_lib, 'active', lambda *a: conn)
    monkeypatch.setattr(setup, 'installed', lambda *a, **kw: verified.append((a, kw)) or {'state': 'ready'})
    assert setup.import_app(conn, app_id=3, installation_id=4, owner_id=7, actor='owner') == {'state': 'ready'}
    assert states[0]['app_id'] == 3 and states[0]['owner_id'] == 7 and states[0]['state_hash'] == ''
    assert verified == [((conn, 4, None), {'actor': 'owner'})]


@pytest.mark.parametrize('mode', ['app', 'pat'])
def test_unexpected_provider_failure_is_a_failed_diagnostic(conn, monkeypatch, mode):
    conn.settings.mode = mode
    monkeypatch.setenv('HUBZOID_PUBLIC_URL', 'http://localhost:3000')

    def failed(*a):
        raise OSError('private connection detail')

    monkeypatch.setattr(setup.gh, 'app_client', failed)
    monkeypatch.setattr(setup.gh, 'for_repo', failed)
    checks = setup.doctor_checks(conn, {})
    assert any(c.status == 'fail' and c.code == 'credential_rejected' for c in checks)
    assert 'private connection' not in str(checks)


def test_apply_hub_file_pauses_intake_until_explicitly_enabled(conn, monkeypatch):
    def missing(*a):
        raise ChannelError('not_found', 'Not applied', status=404)

    data = {'enabled': True, 'provider_config': {}}
    applied = []
    monkeypatch.setattr(setup.config_lib, 'active', missing)
    monkeypatch.setattr(setup.config_lib, 'export', missing)
    monkeypatch.setattr(setup.config_lib, 'file_config', lambda *a: data)
    monkeypatch.setattr(setup.config_lib, 'apply', lambda *a, **kw: applied.append((a, kw)))
    assert setup.apply_hub_file(conn.hub_dir, conn.id, actor='owner') is True
    assert applied[0][0][1]['enabled'] is False
    assert applied[0][1] == {'expected_revision': 'absent', 'actor': 'owner', 'expected_id': 'github'}
    monkeypatch.setattr(setup.config_lib, 'file_config', lambda *a: None)
    assert setup.apply_hub_file(conn.hub_dir, conn.id, actor='owner') is False
