"""MCP access tokens are RFC 9068 JWTs that an outside verifier can check from
the discovery document, while Hubzoid still checks the grant on every call."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt as pyjwt
import pytest

from hubzoid import mcp_signing
from tests.test_mcp_oauth import RESOURCE, consent, exchange, register, rpc, run_flow, setup  # noqa: F401


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


async def _tokens(c):
    client = await register(c)
    q = await consent(c, client)
    r = await exchange(c, client, q["code"][0])
    assert r.status_code == 200, r.text
    return client, r.json()


def _outside_verify(jwks: dict, token: str, audience: str, issuer: str) -> dict:
    """What AWS AgentCore's JWT authorizer does: keys from jwks_uri, then
    signature, issuer, audience and expiry. Uses PyJWT, not Hubzoid's code."""
    keyset = pyjwt.PyJWKSet.from_dict(jwks)
    kid = pyjwt.get_unverified_header(token)["kid"]
    key = next(k for k in keyset.keys if k.key_id == kid)
    return pyjwt.decode(token, key.key, algorithms=["RS256"], audience=audience, issuer=issuer)


def test_access_token_is_a_jwt_an_outside_verifier_accepts(setup):
    hub, _ = setup

    async def flow(c):
        md = (await c.get("/.well-known/oauth-authorization-server/mcp/oauth")).json()
        assert md["jwks_uri"] == RESOURCE + "/oauth/jwks"
        oidc = (await c.get("/mcp/oauth/.well-known/openid-configuration")).json()
        assert oidc["issuer"] == md["issuer"] == RESOURCE + "/oauth"
        assert oidc["jwks_uri"] == md["jwks_uri"]
        assert oidc["id_token_signing_alg_values_supported"] == ["RS256"]
        _client, tokens = await _tokens(c)
        token = tokens["access_token"]
        header = pyjwt.get_unverified_header(token)
        assert header["alg"] == "RS256" and header["typ"] == "at+jwt" and header["kid"]
        jwks = (await c.get("/mcp/oauth/jwks")).json()
        assert all("d" not in k and "p" not in k for k in jwks["keys"])
        claims = _outside_verify(jwks, token, RESOURCE, oidc["issuer"])
        assert claims["sub"] == "u1" and claims["scope"] == "hub:access" and claims["client_id"]
        assert claims["exp"] - claims["iat"] <= 600 and claims["jti"] and claims["hz_grant"]
        assert "email" not in claims and "alice" not in token.lower()
        assert tokens["refresh_token"].startswith("hz_rt_")
        assert (await rpc(c, token)).status_code == 200

    run_flow(hub, flow)


def test_forged_or_mismatched_tokens_are_refused(setup):
    hub, _ = setup

    async def flow(c):
        _client, tokens = await _tokens(c)
        token = tokens["access_token"]
        head, body, sig = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        # alg none, a changed claim with the old signature, a missing signature
        none = _b64({"alg": "none", "typ": "at+jwt"}) + "." + body + "."
        changed = head + "." + _b64({**claims, "sub": "someone-else"}) + "." + sig
        # HS256 "signed" with the public key (algorithm confusion)
        jwks = (await c.get("/mcp/oauth/jwks")).json()
        pub = json.dumps(jwks["keys"][0]).encode()
        hs_signing = _b64({"alg": "HS256", "typ": "at+jwt", "kid": jwks["keys"][0]["kid"]}) + "." + body
        hs = hs_signing + "." + base64.urlsafe_b64encode(
            hmac.new(pub, hs_signing.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        # A real signature for another audience and another issuer
        ring = mcp_signing.ring(hub, 600)
        other_aud = ring.sign({**claims, "aud": "https://evil.example/mcp"})
        other_iss = ring.sign({**claims, "iss": "https://evil.example/mcp/oauth"})
        expired = ring.sign({**claims, "iat": int(time.time()) - 4000, "exp": int(time.time()) - 3000})
        for bad in (none, changed, head + "." + body + ".", hs, other_aud, other_iss, expired):
            assert (await rpc(c, bad)).status_code == 401, bad[:40]
        assert (await rpc(c, token)).status_code == 200

    run_flow(hub, flow)


def test_revoking_the_connection_stops_the_jwt_at_once(setup):
    hub, _ = setup

    async def flow(c):
        client, tokens = await _tokens(c)
        assert (await rpc(c, tokens["access_token"])).status_code == 200
        from mcp.server.auth.provider import AccessToken

        from hubzoid.mcp_oauth import HubOAuth

        await HubOAuth(hub, RESOURCE).revoke_token(
            AccessToken(token=tokens["access_token"], client_id=client, scopes=["hub:access"]))
        # Still signed and unexpired, but the grant behind it is gone.
        assert (await rpc(c, tokens["access_token"])).status_code == 401
        refreshed = await c.post("/mcp/oauth/token", data={
            "client_id": client, "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"], "resource": RESOURCE})
        assert refreshed.status_code >= 400

    run_flow(hub, flow)


def test_rotation_keeps_old_tokens_until_now_and_publishes_both_keys(setup):
    hub, _ = setup

    async def flow(c):
        client, tokens = await _tokens(c)
        old = tokens["access_token"]
        old_kid = pyjwt.get_unverified_header(old)["kid"]
        new_kid = mcp_signing.rotate(hub)
        assert new_kid != old_kid
        kids = {k["kid"] for k in (await c.get("/mcp/oauth/jwks")).json()["keys"]}
        assert {old_kid, new_kid} <= kids
        assert (await rpc(c, old)).status_code == 200
        r = await c.post("/mcp/oauth/token", data={
            "client_id": client, "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"], "resource": RESOURCE})
        fresh = r.json()["access_token"]
        assert pyjwt.get_unverified_header(fresh)["kid"] == new_kid
        mcp_signing.rotate(hub, now=True)
        assert (await rpc(c, old)).status_code == 401
        assert (await rpc(c, fresh)).status_code == 401
        listed = mcp_signing.list_keys(hub)
        assert sum(1 for k in listed if k["retired_at"] is None) == 1

    run_flow(hub, flow)


def test_a_key_from_the_environment_signs(setup, monkeypatch):
    hub, _ = setup
    from joserfc.jwk import RSAKey

    key = RSAKey.generate_key(2048)
    monkeypatch.setenv(mcp_signing.ENV, key.as_pem(private=True).decode())
    mcp_signing.reset_cache()

    async def flow(c):
        _client, tokens = await _tokens(c)
        assert pyjwt.get_unverified_header(tokens["access_token"])["kid"] == key.thumbprint()
        assert (await rpc(c, tokens["access_token"])).status_code == 200

    run_flow(hub, flow)
    mcp_signing.reset_cache()


def test_opaque_tokens_from_before_the_upgrade_still_work(setup):
    hub, _ = setup

    async def flow(c):
        client, tokens = await _tokens(c)
        from hubzoid.mcp_oauth import HubOAuth

        oauth = HubOAuth(hub, RESOURCE)
        grant = pyjwt.decode(tokens["access_token"], options={"verify_signature": False})["hz_grant"]
        legacy = "hz_at_legacy-token-value"
        with oauth.store.engine.begin() as conn:
            oauth.store.put(conn, legacy, "access",
                            {"grant": grant, "client_id": client, "scopes": ["hub:access"]},
                            time.time() + 300)
        assert (await rpc(c, legacy)).status_code == 200
        assert (await rpc(c, "hz_at_unknown")).status_code == 401

    run_flow(hub, flow)


@pytest.fixture(autouse=True)
def _fresh_rings():
    mcp_signing.reset_cache()
    yield
    mcp_signing.reset_cache()


def test_operator_managed_keys_and_previous_keys(setup, monkeypatch):
    hub, _ = setup
    from joserfc.jwk import RSAKey

    old, new = RSAKey.generate_key(2048), RSAKey.generate_key(2048)
    monkeypatch.setenv(mcp_signing.ENV, old.as_pem(private=True).decode())
    mcp_signing.reset_cache()
    holder = {}

    async def first(c):
        holder["client"], holder["tokens"] = await _tokens(c)

    run_flow(hub, first)
    with pytest.raises(mcp_signing.SigningError):
        mcp_signing.rotate(hub)
    monkeypatch.setenv(mcp_signing.ENV, new.as_pem(private=True).decode())
    monkeypatch.setenv(mcp_signing.ENV_PREVIOUS, old.as_pem(private=False).decode())
    mcp_signing.reset_cache()

    async def second(c):
        kids = {k["kid"] for k in (await c.get("/mcp/oauth/jwks")).json()["keys"]}
        assert kids == {old.thumbprint(), new.thumbprint()}
        assert (await rpc(c, holder["tokens"]["access_token"])).status_code == 200

    run_flow(hub, second)
    monkeypatch.delenv(mcp_signing.ENV_PREVIOUS)
    mcp_signing.reset_cache()

    async def third(c):
        assert (await rpc(c, holder["tokens"]["access_token"])).status_code == 401

    run_flow(hub, third)


def test_a_token_whose_subject_disagrees_with_its_grant_is_refused(setup):
    hub, _ = setup

    async def flow(c):
        _client, tokens = await _tokens(c)
        claims = pyjwt.decode(tokens["access_token"], options={"verify_signature": False})
        other = mcp_signing.ring(hub, 600).sign({**claims, "sub": "u2", "jti": "x"})
        assert (await rpc(c, other)).status_code == 401

    run_flow(hub, flow)
