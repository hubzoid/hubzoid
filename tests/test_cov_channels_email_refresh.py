"""Keeping a mailbox token fresh (hubzoid.channels.email.oauth.access_token):
a valid token is used as is; one worker at a time renews (a lease); another
worker waits for the renewal and never gets an expired token; a refused
refresh blocks the mailbox unless a reconnect replaced the grant meanwhile;
a reconnect during a refresh keeps the newer grant; the lease is released.
The secret store, provider and connection state are fakes."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hubzoid.channels.email import oauth
from hubzoid.channels.errors import ChannelError


class World:
    def __init__(self, monkeypatch, tokens, *, generation=0, lease=None):
        self.tokens, self.generation, self.saved, self.posts, self.blocked = dict(tokens), generation, [], [], False
        self.state = {"email_refresh_lease": lease or {}}
        monkeypatch.setattr(oauth, "_tokens", lambda conn: dict(self.tokens))
        monkeypatch.setattr(oauth, "_generation", lambda conn: self.generation)
        monkeypatch.setattr(oauth, "_save", lambda conn, t, actor: self.saved.append(t))
        monkeypatch.setattr(oauth, "_secret", lambda conn, *a: "client-secret")
        monkeypatch.setattr(oauth, "endpoints", lambda conn: {"token": "https://idp/token", "client_id": "cid",
                                                              "secret_field": ("email", "client_secret_ref")})
        monkeypatch.setattr(oauth.time, "sleep", lambda s: None)

        def swap(hub_dir, hub, cid, fn):
            changes, result = fn(dict(self.state))
            if changes:
                self.state.update(changes)
            return result

        def set_state(hub_dir, hub, cid, **kw):
            self.blocked = kw.get("email_blocked", self.blocked)

        monkeypatch.setattr(oauth.store, "swap_connection_state", swap)
        monkeypatch.setattr(oauth.store, "set_connection_state", set_state)

    def provider(self, monkeypatch, answer):
        def post(url, data):
            self.posts.append(data)
            if isinstance(answer, Exception):
                raise answer
            return dict(answer)

        monkeypatch.setattr(oauth, "_post", post)


CONN = SimpleNamespace(hub_dir="/h", hub="desk", id="mail", state={})
FRESH = time.time() + 3600
STALE = time.time() - 10


def test_a_valid_token_is_used_as_is(monkeypatch):
    w = World(monkeypatch, {"access_token": "tok-1", "expires_at": FRESH})
    assert oauth.access_token(CONN) == "tok-1" and w.posts == []
    assert oauth.current_token(CONN) == "tok-1"


def test_an_expiring_token_is_renewed_once_and_saved(monkeypatch):
    w = World(monkeypatch, {"access_token": "old", "refresh_token": "r-1", "expires_at": STALE})
    w.provider(monkeypatch, {"access_token": "new", "expires_in": 1800})
    assert oauth.access_token(CONN) == "new"
    assert w.posts[0]["grant_type"] == "refresh_token" and w.posts[0]["refresh_token"] == "r-1"
    assert w.saved[0]["access_token"] == "new" and w.saved[0]["expires_at"] > time.time() + 1700
    assert w.state["email_refresh_lease"] == {}                      # released
    assert oauth.current_token(CONN) is None                          # stored one is still the stale one


def test_without_a_refresh_token_the_mailbox_must_reconnect(monkeypatch):
    w = World(monkeypatch, {"access_token": "old", "expires_at": STALE})
    with pytest.raises(ChannelError) as err:
        oauth.access_token(CONN)
    assert err.value.code == "auth_required" and w.state["email_refresh_lease"] == {}


def test_a_refused_refresh_blocks_the_mailbox(monkeypatch):
    w = World(monkeypatch, {"access_token": "old", "refresh_token": "r-1", "expires_at": STALE})
    w.provider(monkeypatch, ChannelError("invalid_grant", "refused"))
    with pytest.raises(ChannelError) as err:
        oauth.access_token(CONN)
    assert err.value.code == "auth_revoked" and w.blocked is True
    with pytest.raises(ChannelError) as err:
        oauth.access_token(SimpleNamespace(**{**vars(CONN), "state": {"email_blocked": True}}))
    assert err.value.code == "auth_revoked"


def test_a_reconnect_during_the_refresh_keeps_the_newer_grant(monkeypatch):
    w = World(monkeypatch, {"access_token": "old", "refresh_token": "r-1", "expires_at": STALE})

    def post_then_reconnect(url, data):
        w.generation += 1                                             # someone consented again meanwhile
        w.tokens = {"access_token": "theirs", "expires_at": FRESH}
        return {"access_token": "mine", "expires_in": 60}

    monkeypatch.setattr(oauth, "_post", post_then_reconnect)
    assert oauth.access_token(CONN) == "theirs" and w.saved == []

    w2 = World(monkeypatch, {"access_token": "old", "refresh_token": "r-1", "expires_at": STALE})

    def refused_after_reconnect(url, data):
        w2.generation += 1
        w2.tokens = {"access_token": "theirs-2", "expires_at": FRESH}
        raise ChannelError("invalid_grant", "refused")

    monkeypatch.setattr(oauth, "_post", refused_after_reconnect)
    assert oauth.access_token(CONN) == "theirs-2" and w2.blocked is False


def test_another_worker_waits_and_never_gets_an_expired_token(monkeypatch):
    held = {"owner": "other", "until": time.time() + 30}
    w = World(monkeypatch, {"access_token": "old", "refresh_token": "r-1", "expires_at": STALE}, lease=held)
    with pytest.raises(ChannelError) as err:
        oauth.access_token(CONN)
    assert err.value.code == "auth_refreshing" and w.posts == []
    assert w.state["email_refresh_lease"] == held                     # not ours: not released

    calls = {"n": 0}

    def renewed_meanwhile(conn):
        calls["n"] += 1
        return {"access_token": "renewed", "expires_at": FRESH} if calls["n"] > 2 else dict(w.tokens)

    monkeypatch.setattr(oauth, "_tokens", renewed_meanwhile)
    assert oauth.access_token(CONN) == "renewed"
