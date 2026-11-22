"""Confirming it's you before a sensitive change (POST /api/auth/reauth,
hubzoid.auth.routes): the body must be an object; with two-step sign-in the
password alone is not enough, a wrong code is refused and counted, a recovery
code works once; an account with neither password nor code is sent to its
provider; too many tries wait; an unreachable database is 503, never a
confirmation."""
from __future__ import annotations

import time

from sqlalchemy.exc import OperationalError

from hubzoid.auth import mfa, ratelimit, routes, sessions
from tests.test_auth_mfa import _code, _enroll, client, hub  # noqa: F401
from tests.test_auth_routes import ORIGIN, PASSWORD, person, sign_in


def reauth(c, **body):
    return c.post("/api/auth/reauth", json=body, headers=ORIGIN)


def stale(monkeypatch):
    clock = [time.time() + sessions.RECENT_SECONDS + 5]
    monkeypatch.setattr(sessions.time, "time", lambda: clock[0])


def test_the_body_must_be_an_object(hub):  # noqa: F811
    person(hub)
    c = client(hub)
    sign_in(c)
    r = c.post("/api/auth/reauth", json=["password"], headers=ORIGIN)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_request"


def test_with_two_step_sign_in_the_code_is_needed_too(hub, monkeypatch):  # noqa: F811
    user = person(hub)
    c = client(hub)
    sign_in(c)
    secret, recovery = _enroll(c)
    stale(monkeypatch)
    r = reauth(c, password=PASSWORD, code="000000")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_code"
    r = reauth(c, password=PASSWORD, recovery_code=recovery[0])
    assert r.status_code == 200 and r.json() == {"verified": True}
    assert reauth(c, password=PASSWORD, recovery_code=recovery[0]).status_code == 401   # used once
    assert mfa.enrolled(hub, user["id"])
    assert reauth(c, password=PASSWORD, code=_code(secret, 1)).status_code == 200


def test_an_account_without_password_or_code_confirms_with_its_provider(hub, monkeypatch):  # noqa: F811
    person(hub)
    c = client(hub)
    sign_in(c)
    real = routes.users.get

    def without_password(hub_dir, user_id):
        return {**(real(hub_dir, user_id) or {}), "has_password": False}

    monkeypatch.setattr(routes.users, "get", without_password)
    r = reauth(c, password=PASSWORD)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "reauth_with_provider"
    assert r.json()["detail"]["reauth"] == {"password": False, "code": False, "providers": []}


def test_too_many_tries_wait_and_a_lost_database_never_confirms(hub, monkeypatch):  # noqa: F811
    person(hub)
    c = client(hub)
    sign_in(c)
    monkeypatch.setattr(ratelimit, "admit", lambda hub_dir, ip, email: 42)
    r = reauth(c, password=PASSWORD)
    assert r.status_code == 429

    def database_gone(hub_dir, ip, email):
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    monkeypatch.setattr(ratelimit, "admit", database_gone)
    r = reauth(c, password=PASSWORD)
    assert r.status_code == 503 and r.json() != {"verified": True}
