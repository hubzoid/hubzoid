"""Token storage, refresh and revocation (``hubzoid.connectors.tokens``).

A fake authorization server answers through an in-process httpx transport, so
concurrency and failure paths are exact: several turns share one refresh,
another process's refresh is waited for and tolerated, a replayed refresh
token expires the connection, an unreachable server keeps it for a retry.
"""
from __future__ import annotations

import asyncio
import base64
import threading
import time
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import text

from hubzoid import connectors, secretbox
from hubzoid.connectors import http as net
from hubzoid.connectors import tokens
from tests import connectors_fakes as f

UID, CID = "local-owner", "gmail"
URL = "https://mcp.example.org/mcp"


class FakeAS:
    """Rotating refresh tokens with replay detection, and RFC 7009 revocation."""

    def __init__(self):
        self.valid = {"rt-1"}
        self.n = 1
        self.calls: list[dict] = []
        self.revoked: list[dict] = []
        self.lock = threading.Lock()
        self.delay = 0.0
        self.fail: object = None  # an exception to raise or a status to answer
        self.before = None        # called before answering a refresh

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        entry = {"path": request.url.path, "form": form,
                 "authorization": request.headers.get("authorization")}
        if request.url.path == "/revoke":
            self.revoked.append(entry)
            if isinstance(self.fail, int):
                return httpx.Response(self.fail)
            return httpx.Response(200)
        self.calls.append(entry)
        if isinstance(self.fail, Exception):
            raise self.fail
        if isinstance(self.fail, int):
            return httpx.Response(self.fail, json={"error": "temporarily_unavailable"})
        if self.before:
            self.before()
        time.sleep(self.delay)
        with self.lock:
            rt = form.get("refresh_token")
            if rt not in self.valid:
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": f"bad token {rt}"})
            self.valid.discard(rt)
            self.n += 1
            self.valid.add(f"rt-{self.n}")
            return httpx.Response(200, json={"access_token": f"at-{self.n}", "token_type": "bearer",
                                             "expires_in": 3600, "refresh_token": f"rt-{self.n}"})


@pytest.fixture
def hub(tmp_path, monkeypatch):
    f.clean_env(monkeypatch)
    return f.make_hub(tmp_path, "sales")


@pytest.fixture
def fake(monkeypatch):
    server = FakeAS()
    monkeypatch.setattr(net, "_transport", httpx.MockTransport(server))
    return server


def record(**extra) -> dict:
    return {"v": 1, "kind": "oauth", "access_token": "at-1", "refresh_token": "rt-1",
            "token_type": "Bearer", "scope": "mail", "expires_at": time.time() - 1,
            "token_endpoint": "https://as.example.org/token",
            "revocation_endpoint": "https://as.example.org/revoke",
            "resource": URL, "issuer": "https://as.example.org",
            "client": {"client_id": "cid", "client_secret": "csecret",
                       "auth_method": "client_secret_basic", "source": "pre-registered"},
            "redirect_uri": "https://hub.example.org/oauth/connectors/gmail/callback",
            "url": URL, **extra}


def save(hub, **extra):
    tokens.store(hub, user_id=UID, email=f.OWNER, connector_id=CID, token=record(**extra))


def write_elsewhere(hub, access="at-other", refresh="rt-other"):
    """What another process's successful refresh leaves in the store."""
    rec = tokens.token_record(hub, UID, CID)
    rec.update(access_token=access, refresh_token=refresh, expires_at=time.time() + 3600)
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("UPDATE hz_connector_tokens SET token_enc = :t, expires_at = :e, "
                          "version = version + 1, refresh_lock_until = NULL, status = 'ok' "
                          "WHERE user_id = :u AND connector_id = :c"),
                     {"t": secretbox.encrypt_json(hub, rec), "e": rec["expires_at"],
                      "u": UID, "c": CID})


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def test_tokens_are_encrypted_at_rest_and_listed_without_secrets(hub):
    save(hub, expires_at=time.time() + 3600)
    with connectors.engine(hub).connect() as conn:
        raw = str(conn.execute(text("SELECT * FROM hz_connector_tokens")).fetchall())
    for secret in ("at-1", "rt-1", "csecret"):
        assert secret not in raw
    (conn,) = tokens.for_user(hub, UID)
    assert conn.connector_id == CID and conn.status == "ok" and conn.email == f.OWNER
    assert "at-1" not in repr(conn)
    assert tokens.access_token_for(hub, UID, CID) == "at-1"
    assert tokens.counts(hub) == {CID: 1}


def test_a_new_authorization_replaces_the_old_one(hub):
    save(hub, expires_at=time.time() + 3600)
    first = tokens.get(hub, UID, CID)
    time.sleep(0.01)
    save(hub, access_token="at-new", expires_at=time.time() + 3600)
    second = tokens.get(hub, UID, CID)
    assert second.version == first.version + 1 and second.connected_at > first.connected_at
    assert tokens.access_token_for(hub, UID, CID) == "at-new"


def test_a_token_is_only_used_for_the_server_that_issued_it(hub):
    save(hub, expires_at=time.time() + 3600)
    assert tokens.access_token_for(hub, UID, CID, url=URL) == "at-1"
    assert tokens.access_token_for(hub, UID, CID, url="https://elsewhere.example/mcp") is None


def test_the_account_behind_an_email(hub, monkeypatch):
    from hubzoid import auth

    # sign-in off: the local owner (a real, stable account id since lane A)
    assert tokens.user_id_for(hub, f.OWNER) == auth.local_owner(hub).id
    assert tokens.user_id_for(hub, "someone@example.org") is None
    f.accounts(monkeypatch, hub, {"alice@example.org": ("u-a", "user")})
    f.add_user(hub, "u-p", "pending@example.org", status="pending")
    assert tokens.user_id_for(hub, "Alice@Example.org ") == "u-a"
    assert tokens.user_id_for(hub, "pending@example.org") is None
    assert tokens.user_id_for(hub, f.OWNER) is None  # sign-in on: no implicit owner
    assert tokens.user_id_for(hub, "") is None


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------
def test_refresh_sends_the_resource_and_the_registered_client_auth(hub, fake):
    save(hub)
    assert tokens.access_token_for(hub, UID, CID) == "at-2"
    (call,) = fake.calls
    assert call["form"] == {"grant_type": "refresh_token", "refresh_token": "rt-1",
                            "resource": URL, "client_id": "cid"}
    assert call["authorization"] == "Basic " + base64.b64encode(b"cid:csecret").decode()
    rec = tokens.token_record(hub, UID, CID)
    assert rec["refresh_token"] == "rt-2" and rec["expires_at"] > time.time() + 3000
    assert tokens.get(hub, UID, CID).status == "ok"
    # Fresh now: no further call.
    assert tokens.access_token_for(hub, UID, CID) == "at-2" and len(fake.calls) == 1


@pytest.mark.parametrize("method, secret_in_form, basic", [
    ("client_secret_post", True, False), ("none", False, False)])
def test_other_client_auth_methods(hub, fake, method, secret_in_form, basic):
    save(hub, client={"client_id": "cid", "client_secret": "csecret" if method != "none" else None,
                      "auth_method": method, "source": "dynamic"})
    assert tokens.access_token_for(hub, UID, CID) == "at-2"
    (call,) = fake.calls
    assert ("client_secret" in call["form"]) is secret_in_form
    assert (call["authorization"] is not None) is basic


def test_a_non_rotating_provider_keeps_the_refresh_token(hub, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"access_token": "at-9", "token_type": "Bearer",
                                         "expires_in": 60})
    monkeypatch.setattr(net, "_transport", httpx.MockTransport(handler))
    save(hub)
    assert tokens.access_token_for(hub, UID, CID) == "at-9"
    assert tokens.token_record(hub, UID, CID)["refresh_token"] == "rt-1"


def test_concurrent_turns_share_one_refresh(hub, fake):
    save(hub)
    fake.delay = 0.3
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(
        tokens.access_token_for(hub, UID, CID))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["at-2"] * 8
    assert len(fake.calls) == 1


def test_concurrent_coroutines_share_one_refresh(hub, fake):
    save(hub)
    fake.delay = 0.2

    async def many():
        return await asyncio.gather(*(tokens.access_token_for_async(hub, UID, CID)
                                      for _ in range(6)))

    assert asyncio.run(many()) == ["at-2"] * 6
    assert len(fake.calls) == 1


def test_a_refresh_in_another_process_is_waited_for(hub, fake):
    save(hub)
    # Another process holds the lease and is refreshing right now.
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("UPDATE hz_connector_tokens SET refresh_lock_until = :t"),
                     {"t": time.time() + 30})
    timer = threading.Timer(0.5, write_elsewhere, args=(hub,))
    timer.start()
    try:
        assert tokens.access_token_for(hub, UID, CID) == "at-other"
    finally:
        timer.join()
    assert fake.calls == []  # never spent the same refresh token


def test_a_lease_left_by_a_crashed_process_is_taken_over(hub, fake):
    save(hub)
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("UPDATE hz_connector_tokens SET refresh_lock_until = :t"),
                     {"t": time.time() + 0.4})
    assert tokens.access_token_for(hub, UID, CID) == "at-2"
    assert len(fake.calls) == 1


def test_waiting_gives_up_but_uses_a_still_valid_token(hub, fake, monkeypatch):
    monkeypatch.setattr(tokens, "WAIT_SECONDS", 0.5)
    save(hub, expires_at=time.time() + 30)  # due (within the skew) but still valid
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("UPDATE hz_connector_tokens SET refresh_lock_until = :t"),
                     {"t": time.time() + 30})
    assert tokens.access_token_for(hub, UID, CID) == "at-1"
    assert fake.calls == []


def test_a_concurrent_refresh_elsewhere_is_tolerated(hub, fake):
    """Another process spent the refresh token a moment before us: the provider
    says invalid_grant, but the store already holds its fresh token."""
    save(hub)

    def elsewhere():
        fake.valid.discard("rt-1")
        write_elsewhere(hub)

    fake.before = elsewhere
    assert tokens.access_token_for(hub, UID, CID) == "at-other"
    conn = tokens.get(hub, UID, CID)
    assert conn.status == "ok"


def test_a_replayed_refresh_token_expires_the_connection(hub, fake):
    save(hub, refresh_token="rt-spent")
    assert tokens.access_token_for(hub, UID, CID) is None
    conn = tokens.get(hub, UID, CID)
    assert conn.status == "expired" and conn.error == "refresh_rejected:invalid_grant"
    assert "bad token" not in (conn.error or "")  # the provider's description is never kept
    assert tokens.access_token_for(hub, UID, CID) is None
    assert len(fake.calls) == 1  # an expired connection is not retried


def test_an_unreachable_provider_keeps_the_connection_for_a_retry(hub, fake):
    save(hub)
    fake.fail = httpx.ConnectError("down")
    assert tokens.access_token_for(hub, UID, CID) is None
    conn = tokens.get(hub, UID, CID)
    assert conn.status == "error" and conn.error == "refresh_unavailable:ConnectError"
    fake.fail = 503
    assert tokens.access_token_for(hub, UID, CID) is None
    assert tokens.get(hub, UID, CID).error == "refresh_unavailable:http_503"
    fake.fail = None
    assert tokens.access_token_for(hub, UID, CID) == "at-2"
    assert tokens.get(hub, UID, CID).status == "ok"


def test_an_expired_token_without_a_refresh_token_expires(hub, fake):
    save(hub, refresh_token=None)
    assert tokens.access_token_for(hub, UID, CID) is None
    assert tokens.get(hub, UID, CID).status == "expired"
    assert fake.calls == []


def test_an_undecryptable_token_is_not_used(hub, monkeypatch):
    save(hub, expires_at=time.time() + 3600)
    monkeypatch.setenv("HUBZOID_SECRET_KEY", "Z" * 43 + "=")
    assert tokens.access_token_for(hub, UID, CID) is None


# ---------------------------------------------------------------------------
# Revocation and disconnection
# ---------------------------------------------------------------------------
def test_disconnect_revokes_then_deletes(hub, fake):
    save(hub, expires_at=time.time() + 3600)
    assert tokens.disconnect(hub, UID, CID) is True
    hints = [(r["form"]["token"], r["form"]["token_type_hint"]) for r in fake.revoked]
    assert hints == [("rt-1", "refresh_token"), ("at-1", "access_token")]
    assert all(r["authorization"].startswith("Basic ") for r in fake.revoked)
    assert tokens.get(hub, UID, CID) is None
    assert tokens.disconnect(hub, UID, CID) is False


def test_disconnect_deletes_even_when_revocation_fails(hub, fake):
    save(hub, expires_at=time.time() + 3600)
    fake.fail = 500
    assert tokens.disconnect(hub, UID, CID) is True
    assert tokens.get(hub, UID, CID) is None


def test_revocation_works_with_mcp_sdk_servers(hub, monkeypatch):
    """The MCP Python SDK's revocation endpoint wants a client_secret field even
    from a public client; a standard request is retried once with it."""
    seen = []

    def handler(request):
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        seen.append(sorted(form))
        if "client_secret" not in form:
            return httpx.Response(400, json={"error": "invalid_request"})
        return httpx.Response(200)

    monkeypatch.setattr(net, "_transport", httpx.MockTransport(handler))
    assert tokens.revoke(record(client={"client_id": "cid", "auth_method": "none"})) is True
    assert seen[0] == ["client_id", "token", "token_type_hint"]
    assert seen[1] == ["client_id", "client_secret", "token", "token_type_hint"]


def test_a_provider_without_revocation_is_simply_forgotten(hub, fake):
    save(hub, expires_at=time.time() + 3600, revocation_endpoint=None)
    assert tokens.disconnect(hub, UID, CID) is True
    assert fake.revoked == []


def test_dropping_a_connector_revokes_in_the_background(hub, fake):
    save(hub, expires_at=time.time() + 3600)
    tokens.store(hub, user_id="u-2", email="b@example.org", connector_id=CID,
                 token=record(access_token="at-b", refresh_token="rt-b"))
    assert tokens.drop_connector(hub, CID) == 2
    assert tokens.for_user(hub, UID) == [] and tokens.for_user(hub, "u-2") == []
    deadline = time.time() + 5
    while len(fake.revoked) < 4 and time.time() < deadline:
        time.sleep(0.05)
    assert sorted(r["form"]["token"] for r in fake.revoked) == ["at-1", "at-b", "rt-1", "rt-b"]


def test_a_deleted_account_loses_every_connection(hub, fake):
    save(hub, expires_at=time.time() + 3600)
    tokens.store(hub, user_id=UID, email=f.OWNER, connector_id="cal",
                 token=record(access_token="at-c", refresh_token="rt-c"))
    tokens.store(hub, user_id="u-2", email="b@example.org", connector_id=CID, token=record())
    assert tokens.drop_user(hub, UID) == 2
    assert tokens.for_user(hub, UID) == [] and len(tokens.for_user(hub, "u-2")) == 1
    assert tokens.drop_user(hub, "") == 0


# ---------------------------------------------------------------------------
# Races between a refresh and a new authorization, a disconnect or a slow server
# ---------------------------------------------------------------------------
def test_a_refresh_that_loses_to_a_new_authorization_does_not_revoke(hub, fake):
    """At many providers revoking any token of a grant revokes the whole grant,
    including the person's brand new authorization."""
    save(hub)
    fake.before = lambda: save(hub, access_token="at-new", refresh_token="rt-new",
                               expires_at=time.time() + 3600)
    assert tokens.access_token_for(hub, UID, CID) == "at-new"
    time.sleep(0.3)
    assert fake.revoked == []
    assert tokens.token_record(hub, UID, CID)["refresh_token"] == "rt-new"


def test_a_refresh_that_loses_to_a_disconnect_revokes_its_new_tokens(hub, fake):
    save(hub)

    def removed():
        with connectors.engine(hub).begin() as conn:
            conn.execute(text("DELETE FROM hz_connector_tokens"))

    fake.before = removed
    assert tokens.access_token_for(hub, UID, CID) is None
    deadline = time.time() + 5
    while len(fake.revoked) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert sorted(r["form"]["token"] for r in fake.revoked) == ["at-2", "rt-2"]
    assert tokens.get(hub, UID, CID) is None


def test_disconnect_waits_for_a_refresh_and_revokes_what_it_deletes(hub, fake):
    save(hub, expires_at=time.time() + 3600)
    with connectors.engine(hub).begin() as conn:  # another process is refreshing
        conn.execute(text("UPDATE hz_connector_tokens SET refresh_lock_until = :t"),
                     {"t": time.time() + 30})
    timer = threading.Timer(0.5, write_elsewhere, args=(hub,))
    timer.start()
    try:
        assert tokens.disconnect(hub, UID, CID) is True
    finally:
        timer.join()
    assert [r["form"]["token"] for r in fake.revoked] == ["rt-other", "at-other"]
    assert tokens.get(hub, UID, CID) is None


def test_a_new_authorization_replaces_the_record_whole(hub, fake):
    """Review finding 3: no refresh token is carried over from an earlier
    authorization, even for the same client and issuer: the new one may be
    another remote account. A refresh in progress elsewhere is not waited for:
    it loses its compare-and-set (review finding 5)."""
    save(hub, issuer="https://as.example.org")
    with connectors.engine(hub).begin() as conn:  # a refresh is spending rt-1 right now
        conn.execute(text("UPDATE hz_connector_tokens SET refresh_lock_until = :t"),
                     {"t": time.time() + 30})
    began = time.monotonic()
    tokens.store(hub, user_id=UID, email=f.OWNER, connector_id=CID,
                 token=record(access_token="at-new", refresh_token=None,
                              expires_at=time.time() + 3600))
    assert time.monotonic() - began < 2
    stored = tokens.token_record(hub, UID, CID)
    assert stored["access_token"] == "at-new" and stored["refresh_token"] is None
    assert tokens.access_token_for(hub, UID, CID) == "at-new"
    # The lease is released: the running refresh loses its compare-and-set.
    with connectors.engine(hub).connect() as conn:
        lease = conn.execute(text("SELECT refresh_lock_until FROM hz_connector_tokens")).scalar()
    assert lease is None


def test_a_refresh_request_cannot_outlive_its_lease(hub, monkeypatch):
    monkeypatch.setattr(tokens, "REFRESH_DEADLINE", 0.5)

    def trickle():
        yield b'{"access_token": "at-slow", '
        time.sleep(1.0)
        yield b'"token_type": "Bearer"}'

    monkeypatch.setattr(net, "_transport", httpx.MockTransport(
        lambda request: httpx.Response(200, content=trickle())))
    save(hub)
    assert tokens.access_token_for(hub, UID, CID) is None
    conn = tokens.get(hub, UID, CID)
    assert conn.status == "error" and conn.error == "refresh_unavailable:ReadTimeout"
