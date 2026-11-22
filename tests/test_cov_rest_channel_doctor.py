"""Read-only channel diagnostics distinguish missing access and failed checks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hubzoid.channels import doctor, links, router, store
from hubzoid.channels.errors import ChannelError
from hubzoid.channels.protocol import Check


@pytest.fixture
def conn(tmp_path, monkeypatch):
    from hubzoid.auth import users

    monkeypatch.setattr(links, 'workspace_of', lambda *a: 'workspace')
    monkeypatch.setattr(store, 'resolve_link', lambda *a: {'account_email': 'a@example.org', 'account_id': 'u1'})
    monkeypatch.setattr(users, 'get', lambda *a: {'status': 'active'})
    monkeypatch.setattr(router, '_hub_access', lambda *a: None)
    monkeypatch.setattr(router, '_groups_ok', lambda *a: True)
    return SimpleNamespace(hub_dir=tmp_path, hub='desk', provider='linear', id='main', revision=1, enabled=True,
        config=SimpleNamespace(agent='main', workflow=None, allowed_groups=[], model_dump=lambda: {}),
        settings=SimpleNamespace(token_ref='env:TOKEN'), secret=lambda *a: 'token',
        adapter=SimpleNamespace(secret_keys=('token_ref',), receives_webhooks=lambda c: True,
            customer_facing=False, doctor=lambda *a: [Check('provider', 'ok', 'Read succeeded')]))


def test_actor_workspace_failure_is_not_an_identity_match(conn, monkeypatch):
    def failed(*a):
        raise ChannelError('workspace_required', 'Choose a workspace')

    monkeypatch.setattr(links, 'workspace_of', failed)
    checks = doctor._actor_checks(conn, 'external', None)
    assert [(c.id, c.status, c.code) for c in checks] == [('actor', 'fail', 'workspace_required')]


@pytest.mark.parametrize('link,user,hub_denied,granted,expected', [
    (None, {}, None, True, [('actor:link', 'fail')]),
    ({'account_id': 'u1', 'account_email': 'a@example.org'}, None, None, True,
     [('actor:link', 'ok'), ('actor:account', 'fail')]),
    ({'account_id': 'u1', 'account_email': 'a@example.org'}, {'status': 'blocked'}, None, True,
     [('actor:link', 'ok'), ('actor:account', 'fail')]),
    ({'account_id': 'u1', 'account_email': 'a@example.org'}, {'status': 'active'}, 'permission_denied', False,
     [('actor:link', 'ok'), ('actor:hub', 'fail'), ('actor:grant', 'fail')]),
    ({'account_id': 'u1', 'account_email': 'a@example.org'}, {'status': 'active'}, None, True,
     [('actor:link', 'ok'), ('actor:hub', 'ok'), ('actor:grant', 'ok')]),
])
def test_actor_checks_current_account_and_grants(conn, monkeypatch, link, user, hub_denied, granted, expected):
    from hubzoid.auth import users

    monkeypatch.setattr(store, 'resolve_link', lambda *a: link)
    monkeypatch.setattr(users, 'get', lambda *a: user)
    monkeypatch.setattr(router, '_hub_access', lambda *a: hub_denied)
    monkeypatch.setattr(router, '_groups_ok', lambda *a: granted)
    checks = doctor._actor_checks(conn, 'external', None)
    assert [(c.id, c.status) for c in checks] == expected


@pytest.mark.parametrize('base,status,code', [('', 'fail', 'public_url_required'),
    ('http://localhost:3000', 'warn', 'public_url_required'),
    ('http://public.example.org', 'fail', 'public_url_unsafe'),
    ('https://public.example.org', 'ok', '')])
def test_public_url_must_be_reachable_https(conn, monkeypatch, base, status, code):
    monkeypatch.setenv('HUBZOID_PUBLIC_URL', base)
    check = doctor._public_url_check(conn)
    assert check.status == status and check.code == code
    if status == 'ok':
        assert '/api/hubs/desk/channels/main/webhook' in check.message


@pytest.mark.parametrize('failure,code', [('channel', 'reauth_required'), ('unexpected', 'provider_unavailable')])
def test_doctor_provider_errors_cannot_make_ready(conn, monkeypatch, failure, code):
    monkeypatch.setenv('HUBZOID_PUBLIC_URL', 'https://public.example.org')
    monkeypatch.setattr(doctor.config_lib, 'export', lambda *a: {'file_matches': False, 'file': 'channels/main.yaml',
                                                               'revision': 1})
    monkeypatch.setattr(doctor.config_lib, 'validate', lambda *a: SimpleNamespace(errors=[]))
    monkeypatch.setattr(doctor, '_holders', lambda *a: 0)
    monkeypatch.setattr(store, 'list_links', lambda *a: [])
    monkeypatch.setattr(store, 'counts', lambda *a: {'last_delivery_at': None})

    def failed(*a):
        if failure == 'channel':
            raise ChannelError(code, 'Reconnect app', recovery='Run connect')
        raise OSError('credential material must not appear')

    conn.adapter.doctor = failed
    out = doctor.run(conn, {'actor': 'external'})
    by_id = {c['id']: c for c in out['checks']}
    assert out['ready'] is False and out['network_writes'] is False
    assert by_id['provider']['status'] == 'fail' and by_id['provider']['code'] == code
    assert by_id['file']['code'] == 'file_not_applied' and by_id['grants']['code'] == 'nobody_granted'
    assert 'credential material' not in str(out)


def test_hub_environment_reads_deployment_then_hub_then_process(tmp_path, monkeypatch):
    from hubzoid import deployfile

    hub = tmp_path / 'desk'
    hub.mkdir()
    (tmp_path / '.env').write_text('REST_ENV_VALUE=deployment\nREST_DEPLOY_ONLY=yes\n')
    (hub / '.env').write_text('REST_ENV_VALUE=hub\nREST_HUB_ONLY=yes\n')
    monkeypatch.setattr(deployfile, 'owning', lambda h: tmp_path / 'hubzoid.yaml')
    monkeypatch.setenv('REST_ENV_VALUE', 'process')
    env = doctor._hub_env(hub)
    assert env['REST_ENV_VALUE'] == 'process' and env['REST_DEPLOY_ONLY'] == 'yes'
    assert env['REST_HUB_ONLY'] == 'yes' and env['HUBZOID_GATEWAY'] == '1'
