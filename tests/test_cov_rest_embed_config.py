"""Embed and machine-app configuration rejects unsafe or ambiguous settings."""
from __future__ import annotations

import pytest

from hubzoid.embed import config

PASS = {'secret_ref': 'env:APP_SECRET', 'access': {'agents': ['desk']}}
JWT = {'mode': 'jwt', 'issuer': 'issuer', 'audience': 'hub', 'jwks_url': 'https://idp.example.org/keys',
       'access': {'agents': ['desk']}, 'all_users': True}


@pytest.mark.parametrize('patch,message', [
    ({'mode': 'unknown'}, 'mode is pass or jwt'), ({'origins': 1}, 'origins is a list'),
    ({'access': []}, 'has agents and tools'), ({'access': {'agents': [1]}}, 'list of names'),
    ({'max_lifetime': True}, 'max_lifetime'), ({'max_lifetime': 59}, 'max_lifetime'),
    ({'secret_ref': 1}, 'secret reference'), ({'secret_ref': 'not-a-reference'}, 'secret_ref'),
    ({'public_key': 'not PEM'}, 'PEM public key'), ({'issuer': 'other'}, 'issued by the app itself'),
    ({'audience': ''}, 'audience'), ({'user_claim': ''}, 'user_claim'),
    ({'all_users': 'yes'}, 'all_users is true or false'), ({'all_users': True}, 'for jwt mode'),
    ({'token_to': ['*']}, 'token_to'), ({'token_header': 'bad\r\nheader'}, 'HTTP header name'),
    ({'enabled': 'yes'}, 'enabled is true or false'),
])
def test_pass_config_refusals_name_the_key(patch, message):
    with pytest.raises(ValueError, match=message):
        config.parse({'erp': {**PASS, **patch}})


@pytest.mark.parametrize('patch,message', [
    ({'secret_ref': 'env:SECRET'}, 'exactly one'), ({'issuer': ''}, 'issuer'),
    ({'all_users': False}, 'say how access is decided'),
    ({'scopes': {'map': {}}}, 'use scopes or all_users'),
    ({'all_users': False, 'scopes': []}, 'scopes has claim'),
    ({'all_users': False, 'scopes': {'claim': '', 'map': {}}}, 'scopes.claim'),
])
def test_jwt_access_rule_must_be_explicit(patch, message):
    with pytest.raises(ValueError, match=message):
        config.parse({'erp': {**JWT, **patch}})


@pytest.mark.parametrize('value', [None, [], {'unknown': True}])
def test_machine_api_requires_exact_mapping(value):
    with pytest.raises(ValueError, match='api has account, operations and require'):
        config.parse({'ci': {**JWT, 'api': value}})


def test_machine_api_does_not_accept_embed_settings():
    api = {'account': 'bot@example.org', 'operations': ['chat'], 'require': {'sub': 'repo:one'}}
    with pytest.raises(ValueError, match="doesn't embed the chat"):
        config.parse({'ci': {**JWT, 'api': api}})
    with pytest.raises(ValueError, match='uses mode: jwt'):
        config.parse({'ci': {**PASS, 'api': api}})


@pytest.mark.parametrize('value', ['https://[broken', 'https:///no-host', 'https://example.org/path?q=1'])
def test_origin_parsing_fails_closed(value):
    assert config._origin_ok(value) is False


def test_name_normalization_and_scope_mapping_preserve_tool_case():
    app = config.parse({'erp': {**JWT, 'all_users': False, 'origins': 'https://erp.example.org/',
        'scopes': {'map': {'read': {'agents': ' Desk ', 'tools': ['MyTool']}}}, 'token_to': 'MyTool'}})['erp']
    assert app.origins == ('https://erp.example.org',) and app.token_to == ('MyTool',)
    assert app.scope_map['read'].agents == frozenset({'desk'})
    assert app.scope_map['read'].tools == frozenset({'MyTool'})
    assert config._secure_url('https://[broken') is False
    assert config._secure_url('https:///no-host') is False
