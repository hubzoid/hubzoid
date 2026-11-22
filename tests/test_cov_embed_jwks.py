"""An app's published signing keys (hubzoid.embed.tokens, jwks_url): fetched
over https without redirects and size-capped; symmetric or encryption keys
from a URL are never used; the keys are cached; an unknown key id fetches once
more (not more than once a minute); while the URL fails, keys up to an hour
old keep working and then every token is refused with 503. Also: a missing
secret or an unreadable public key refuses every token (503). The URL is a
fake; the keys are real."""
from __future__ import annotations

import json
import time

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from hubzoid.embed import config, tokens
from hubzoid.embed.config import AppsError

URL = "https://idp.example.com/.well-known/jwks.json"


def _hub(tmp_path, apps_yaml):
    hub = tmp_path / "finance"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: finance\n---\nHelp.\n")
    (tmp_path / "hubzoid.yaml").write_text("version: 1\nhubs: [finance]\napps:\n" + apps_yaml)
    config.reset()
    tokens.reset_cache()
    return hub


JWKS_APP = f"""
  portal:
    mode: jwt
    issuer: https://idp.example.com/
    audience: https://hubzoid.example.com
    jwks_url: {URL}
    all_users: true
    access: {{agents: [finance], tools: ["*"]}}
"""


def _key(kid):
    return RSAKey.generate_key(2048, parameters={"kid": kid})


def _token(key, kid="k1"):
    now = int(time.time())
    return jwt.encode({"alg": "RS256", "kid": kid}, {"iss": "https://idp.example.com/",
                      "aud": "https://hubzoid.example.com", "sub": "user-7", "iat": now, "exp": now + 300}, key)


class Idp:
    """The identity provider's key URL: what it serves and how often it was asked."""

    def __init__(self, monkeypatch, *keys, status=200, body=None):
        self.keys, self.status, self.body, self.asked, self.redirects = list(keys), status, body, 0, []
        idp = self

        class Response:
            def __init__(self):
                self.status_code = idp.status

            def iter_bytes(self):
                data = idp.body if idp.body is not None else json.dumps(
                    {"keys": [k.as_dict(private=False) for k in idp.keys]}).encode()
                for i in range(0, len(data), 4096):
                    yield data[i:i + 4096]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Client:
            def __init__(self, timeout, follow_redirects):
                idp.redirects.append(follow_redirects)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def stream(self, method, url, headers):
                assert method == "GET" and url == URL
                idp.asked += 1
                if isinstance(idp.status, Exception):
                    raise idp.status
                return Response()

        import httpx
        monkeypatch.setattr(httpx, "Client", Client)


def refused(hub, token):
    with pytest.raises(AppsError) as err:
        tokens.verify(hub, token)
    return err.value


def test_keys_are_fetched_without_redirects_and_cached(tmp_path, monkeypatch):
    hub = _hub(tmp_path, JWKS_APP)
    k1 = _key("k1")
    idp = Idp(monkeypatch, k1)
    assert tokens.verify(hub, _token(k1)).user_id == "user-7"
    assert tokens.verify(hub, _token(k1)).user_id == "user-7"
    assert idp.asked == 1 and idp.redirects == [False]


def test_an_unknown_key_id_fetches_once_more_then_refuses(tmp_path, monkeypatch):
    hub = _hub(tmp_path, JWKS_APP)
    k1, k2 = _key("k1"), _key("k2")
    idp = Idp(monkeypatch, k1)
    tokens.verify(hub, _token(k1))
    stranger = _token(_key("k9"), kid="k9")
    err = refused(hub, stranger)
    # Fetched just now: a refresh within a minute reuses what we have.
    assert err.code == "invalid_token" and "not one of the app's keys" in err.message and idp.asked == 1

    tokens._jwks[URL] = (time.time(), time.time() - 120, tokens._jwks[URL][2])
    idp.keys.append(k2)                                          # the provider rotated keys
    assert tokens.verify(hub, _token(k2, kid="k2")).user_id == "user-7" and idp.asked == 2
    assert refused(hub, stranger).code == "invalid_token" and idp.asked == 2


def test_stale_keys_work_for_an_hour_while_the_url_fails(tmp_path, monkeypatch):
    hub = _hub(tmp_path, JWKS_APP)
    k1 = _key("k1")
    idp = Idp(monkeypatch, k1)
    tokens.verify(hub, _token(k1))
    fetched = time.time() - 1200                                 # older than fresh, younger than stale
    tokens._jwks[URL] = (fetched, fetched, tokens._jwks[URL][2])
    idp.status = 500
    assert tokens.verify(hub, _token(k1)).user_id == "user-7" and idp.asked == 2
    assert tokens._jwks[URL][0] == fetched                       # still the old fetch time

    old = time.time() - 4000
    tokens._jwks[URL] = (old, old, tokens._jwks[URL][2])
    err = refused(hub, _token(k1))
    assert err.status == 503 and err.code == "app_unavailable" and "can't be fetched" in err.message


@pytest.mark.parametrize("serve,why", [
    ({"status": 404}, "status"),
    ({"body": b"{" + b" " * (300 * 1024)}, "too large"),
    ({"body": json.dumps({"keys": [OctKey.generate_key(256).as_dict(private=True)]}).encode()}, "symmetric"),
    ({"body": json.dumps({"keys": [dict(_key("e").as_dict(private=False), use="enc")]}).encode()}, "enc"),
    ({"body": json.dumps({"keys": [{"kty": "RSA", "n": "x"}, "junk"]}).encode()}, "unreadable"),
    ({"status": ConnectionError("down")}, "network"),
])
def test_an_unusable_key_url_refuses_every_token(tmp_path, monkeypatch, serve, why):
    hub = _hub(tmp_path, JWKS_APP)
    Idp(monkeypatch, **serve)
    err = refused(hub, _token(_key("k1")))
    assert err.status == 503 and err.code == "app_unavailable", why


def test_a_single_key_needs_no_key_id(tmp_path, monkeypatch):
    hub = _hub(tmp_path, JWKS_APP)
    k1 = _key("k1")
    Idp(monkeypatch, k1)
    now = int(time.time())
    token = jwt.encode({"alg": "RS256"}, {"iss": "https://idp.example.com/", "aud": "https://hubzoid.example.com",
                                          "sub": "user-7", "iat": now, "exp": now + 300}, k1)
    assert tokens.verify(hub, token).user_id == "user-7"


def test_a_missing_secret_or_unreadable_public_key_refuses_every_token(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_SUCH_APP_SECRET", raising=False)
    hub = _hub(tmp_path, """
  erpnext:
    secret_ref: env:NO_SUCH_APP_SECRET
    origins: [https://erp.example.com]
    access: {agents: [finance], tools: ["*"]}
""")
    now = int(time.time())
    token = jwt.encode({"alg": "HS256"}, {"iss": "erpnext", "aud": "hubzoid", "sub": "a", "iat": now,
                                          "exp": now + 60}, OctKey.import_key(b"s" * 40))
    err = refused(hub, token)
    assert err.status == 503 and "not set up" in err.message

    hub2 = tmp_path / "other"
    hub2.mkdir()
    hub2 = _hub(tmp_path / "other", """
  portal:
    mode: jwt
    issuer: https://idp.example.com/
    audience: https://hubzoid.example.com
    public_key: |
      -----BEGIN PUBLIC KEY-----
      bm90IGEga2V5
      -----END PUBLIC KEY-----
    all_users: true
    access: {agents: [finance], tools: ["*"]}
""")
    err = refused(hub2, _token(_key("k1")))
    assert err.status == 503 and "public key can't be read" in err.message
