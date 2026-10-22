"""Apps that embed the chat: the apps section and token checks (hubzoid.embed)."""
from __future__ import annotations

import time

import pytest
from joserfc import jwt
from joserfc.jwk import ECKey, OctKey, RSAKey

from hubzoid.embed import config, tokens
from hubzoid.embed.caller import is_app_subject, subject
from hubzoid.embed.config import AppsError

SECRET = "s" * 40


def _hub(tmp_path, apps_yaml: str):
    hub = tmp_path / "finance"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: finance\n---\nHelp.\n")
    (tmp_path / "hubzoid.yaml").write_text("version: 1\nhubs: [finance]\napps:\n" + apps_yaml)
    config.reset()
    tokens.reset_cache()
    return hub


PASS_APP = """
  erpnext:
    secret_ref: env:ERP_APP_SECRET
    origins: [https://erp.example.com]
    access: {agents: [finance], tools: [erp_reads, erp_reports, "connector:erp"]}
    token_to: ["connector:erp"]
"""


@pytest.fixture
def pass_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_APP_SECRET", SECRET)
    monkeypatch.delenv("HUBZOID_DEPLOYMENT_FILE", raising=False)
    return _hub(tmp_path, PASS_APP)


def _pass(claims: dict | None = None, *, key: str = SECRET, alg: str = "HS256", **over) -> str:
    now = int(time.time())
    body = {"iss": "erpnext", "aud": "hubzoid", "sub": "alice", "name": "Alice", "email": "alice@acme.com",
            "iat": now, "exp": now + 600, "access": {"agent": "finance", "tools": ["erp_reads"]},
            "app_token": "erp-token-alice"}
    body.update(claims or {})
    for k, v in over.items():
        if v is None:
            body.pop(k, None)
        else:
            body[k] = v
    return jwt.encode({"alg": alg}, body, OctKey.import_key(key.encode()))


def test_a_pass_names_the_person_and_narrows_access(pass_hub):
    caller = tokens.verify(pass_hub, _pass())
    assert caller.app_id == "erpnext" and caller.user_id == "alice"
    assert caller.subject == subject("erpnext", "alice") and is_app_subject(caller.subject)
    assert "@" not in caller.subject and len(caller.subject) <= 64
    assert caller.access.agents == {"finance"} and caller.access.tools == {"erp_reads"}
    assert caller.app_token == "erp-token-alice"
    assert "erp-token-alice" not in repr(caller)


def test_a_pass_can_never_widen_the_maximum(pass_hub):
    caller = tokens.verify(pass_hub, _pass(access={"agents": ["finance", "hr"], "tools": ["erp_reads", "delete_all"]}))
    assert caller.access.agents == {"finance"} and caller.access.tools == {"erp_reads"}
    full = tokens.verify(pass_hub, _pass(access=None))
    assert full.access.tools == {"erp_reads", "erp_reports", "connector:erp"}
    none = tokens.verify(pass_hub, _pass(access={"tools": []}))
    assert none.access.tools == frozenset()


@pytest.mark.parametrize("token,code", [
    (lambda: _pass(exp=int(time.time()) - 120, iat=int(time.time()) - 600), "token_expired"),
    (lambda: _pass(key="x" * 40), "invalid_token"),
    (lambda: _pass(iss="other"), "unknown_app"),
    (lambda: _pass(aud="someone-else"), "unknown_app"),
    (lambda: _pass(sub=None), "invalid_token"),
    (lambda: _pass(sub=42), "invalid_token"),
    (lambda: _pass(sub=""), "invalid_token"),
    (lambda: _pass(exp=int(time.time()) + 7200), "invalid_token"),
    (lambda: _pass(iat=int(time.time()) + 3600, exp=int(time.time()) + 3700), "invalid_token"),
    (lambda: _pass(iat=None), "invalid_token"),
    (lambda: _pass(access={"agent": "hr"}), "no_access"),
    (lambda: "not-a-token", "invalid_token"),
    (lambda: "a.b.c", "invalid_token"),
])
def test_bad_passes_are_refused(pass_hub, token, code):
    with pytest.raises(AppsError) as exc:
        tokens.verify(pass_hub, token())
    assert exc.value.code == code


def test_an_unsigned_token_is_refused(pass_hub):
    import base64
    import json

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    now = int(time.time())
    token = f"{seg({'alg': 'none'})}.{seg({'iss': 'erpnext', 'aud': 'hubzoid', 'sub': 'a', 'iat': now, 'exp': now + 60})}."
    with pytest.raises(AppsError) as exc:
        tokens.verify(pass_hub, token)
    assert exc.value.code == "invalid_token"


def test_a_disabled_app_refuses_its_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_APP_SECRET", SECRET)
    hub = _hub(tmp_path, PASS_APP + "    enabled: false\n")
    with pytest.raises(AppsError) as exc:
        tokens.verify(hub, _pass())
    assert exc.value.code == "app_disabled"


def test_a_short_secret_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_APP_SECRET", "short")
    hub = _hub(tmp_path, PASS_APP)
    with pytest.raises(AppsError) as exc:
        tokens.verify(hub, _pass(key="short" * 8))
    assert exc.value.code == "app_unavailable"


def _rsa_hub(tmp_path, monkeypatch, extra: str):
    key = RSAKey.generate_key(2048)
    pem = key.as_pem(private=False).decode()
    indented = "\n".join("      " + line for line in pem.strip().splitlines())
    hub = _hub(tmp_path, f"""
  portal:
    mode: jwt
    issuer: https://idp.example.com/
    audience: https://hubzoid.example.com
    public_key: |
{indented}
    access: {{agents: [finance], tools: ["*"]}}
{extra}""")
    return hub, key, pem


def _app_jwt(key, **claims):
    now = int(time.time())
    body = {"iss": "https://idp.example.com/", "aud": "https://hubzoid.example.com", "sub": "user-7",
            "iat": now, "exp": now + 300}
    body.update(claims)
    alg = "ES256" if isinstance(key, ECKey) else "RS256"
    return jwt.encode({"alg": alg}, body, key)


def test_the_apps_own_jwt_with_mapped_scopes(tmp_path, monkeypatch):
    hub, key, _ = _rsa_hub(tmp_path, monkeypatch,
                           '    scopes: {claim: scope, map: {"chat:finance": {agents: [finance], tools: [erp_reads]}}}\n')
    token = _app_jwt(key, scope="openid chat:finance")
    caller = tokens.verify(hub, token)
    assert caller.access.agents == {"finance"} and caller.access.tools == {"erp_reads"}
    assert caller.app_token == token  # the app's own JWT goes to its tools
    with pytest.raises(AppsError) as exc:
        tokens.verify(hub, _app_jwt(key, scope="openid"))
    assert exc.value.code == "no_access"
    with pytest.raises(AppsError) as exc:  # no scope claim at all: nothing granted (as Agno)
        tokens.verify(hub, _app_jwt(key))
    assert exc.value.code == "no_access"


def test_all_users_is_an_explicit_choice(tmp_path, monkeypatch):
    hub, key, _ = _rsa_hub(tmp_path, monkeypatch, "    all_users: true\n")
    assert tokens.verify(hub, _app_jwt(key)).access.tools == {"*"}


def test_a_public_key_is_never_used_as_an_hmac_secret(tmp_path, monkeypatch):
    hub, key, pem = _rsa_hub(tmp_path, monkeypatch, "    all_users: true\n")
    import base64
    import hashlib
    import hmac
    import json

    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    now = int(time.time())
    signing = seg({"alg": "HS256", "typ": "JWT"}) + "." + seg(
        {"iss": "https://idp.example.com/", "aud": "https://hubzoid.example.com", "sub": "attacker",
         "iat": now, "exp": now + 60})
    sig = hmac.new(pem.encode(), signing.encode(), hashlib.sha256).digest()
    forged = signing + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    with pytest.raises(AppsError) as exc:
        tokens.verify(hub, forged)
    assert exc.value.code == "invalid_token"


def test_two_apps_on_one_identity_provider_are_told_apart_by_audience(tmp_path, monkeypatch):
    key = RSAKey.generate_key(2048)
    pem = "\n".join("      " + line for line in key.as_pem(private=False).decode().strip().splitlines())
    hub = _hub(tmp_path, f"""
  sales:
    mode: jwt
    issuer: https://idp.example.com/
    audience: sales-app
    public_key: |
{pem}
    all_users: true
    access: {{agents: [finance], tools: [sales_tool]}}
  hr:
    mode: jwt
    issuer: https://idp.example.com/
    audience: hr-app
    public_key: |
{pem}
    all_users: true
    access: {{agents: [finance], tools: [hr_tool]}}
""")
    now = int(time.time())
    for aud, tool in (("sales-app", "sales_tool"), ("hr-app", "hr_tool")):
        token = jwt.encode({"alg": "RS256"}, {"iss": "https://idp.example.com/", "aud": aud, "sub": "u",
                                              "iat": now, "exp": now + 60}, key)
        assert tokens.verify(hub, token).access.tools == {tool}
    both = jwt.encode({"alg": "RS256"}, {"iss": "https://idp.example.com/", "aud": ["sales-app", "hr-app"],
                                         "sub": "u", "iat": now, "exp": now + 60}, key)
    with pytest.raises(AppsError) as exc:  # matches two apps: refused, never guessed
        tokens.verify(hub, both)
    assert exc.value.code == "unknown_app"
    # The same person in two apps is two separate subjects.
    assert subject("sales", "u") != subject("hr", "u")


@pytest.mark.parametrize("bad,message", [
    ("  x:\n    secret_ref: env:A\n    access: {agents: [finance]}\n", "app id"),
    ("  erp:\n    access: {agents: [finance]}\n", "secret_ref"),
    ("  erp:\n    secret_ref: env:A\n", "access.agents"),
    ("  erp:\n    secret_ref: env:A\n    access: {agents: [finance]}\n    origins: [http://erp.example.com]\n", "origins"),
    ("  erp:\n    mode: jwt\n    issuer: i\n    audience: a\n    jwks_url: https://x/j\n    access: {agents: [finance]}\n",
     "all_users"),
    ("  erp:\n    mode: jwt\n    issuer: i\n    audience: a\n    jwks_url: http://idp.example.com/j\n"
     "    all_users: true\n    access: {agents: [finance]}\n", "https"),
    ("  erp:\n    secret_ref: env:A\n    access: {agents: [finance]}\n    token_header: X-Hubzoid-User\n", "identity header"),
    ("  erp:\n    secret_ref: env:A\n    access: {agents: [finance]}\n    surprise: 1\n", "unknown key"),
])
def test_an_invalid_apps_section_names_the_problem(tmp_path, bad, message):
    _hub(tmp_path, bad)
    with pytest.raises(AppsError) as exc:
        config.service_for(tmp_path / "finance").all()
    assert message in exc.value.message
