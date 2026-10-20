"""Two-step sign-in: authenticator codes, recovery codes, the sign-in step,
required set-up for administrators, recent sign-in proof, devices and the
admin reset."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid.auth import mfa, routes, sessions, users
from hubzoid.auth import settings as auth_settings

from tests.test_auth_routes import ENV, ORIGIN, PASSWORD, person, sign_in


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    monkeypatch.setenv("HUBZOID_MFA_REQUIRED", "none")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: Sales Helper\n---\nHelp.\n")
    sessions.reset_cache()
    auth_settings.clear_cache()
    yield d
    sessions.reset_cache()
    auth_settings.clear_cache()


def client(hub) -> TestClient:
    app = FastAPI()
    routes.mount(app, hub)
    return TestClient(app)


def _code(secret: str, offset: int = 0) -> str:
    return mfa.code_at(secret, int(time.time() // mfa.STEP) + offset)


def _enroll(c) -> tuple[str, list[str]]:
    started = c.post("/api/me/mfa/enroll", headers=ORIGIN)
    assert started.status_code == 200, started.text
    secret = started.json()["setup"]["key"].replace(" ", "")
    assert started.json()["setup"]["qr"].startswith("data:image/svg+xml;base64,")
    done = c.post("/api/me/mfa/confirm", json={"code": _code(secret)}, headers=ORIGIN)
    assert done.status_code == 200, done.text
    return secret, done.json()["recovery_codes"]


def test_rfc6238_vector():
    # RFC 6238 appendix B, SHA-1, T = 59 s -> 94287082 (8 digits); 6 digits: 287082.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # gitleaks:allow (RFC 6238 test vector)
    assert mfa.code_at(secret, 59 // 30) == "287082"


def test_enrol_then_sign_in_with_a_code(hub):
    person(hub)
    c = client(hub)
    assert sign_in(c).json()["state"] == "signed_in"
    secret, codes = _enroll(c)
    assert len(codes) == 10 and len(set(codes)) == 10
    c.cookies.clear()
    first = sign_in(c)
    assert first.status_code == 200 and first.json()["state"] == "mfa_required"
    assert sessions.SESSION_COOKIE not in first.cookies  # no session before the code
    assert c.get("/api/me/security").status_code == 401
    wrong = c.post("/api/auth/mfa/verify", json={"code": "000000"}, headers=ORIGIN)
    assert wrong.status_code == 401 and wrong.json()["detail"]["tries_left"] == 4
    # The confirming code's step was consumed at set-up; the next step works.
    ok = c.post("/api/auth/mfa/verify", json={"code": _code(secret, 1)}, headers=ORIGIN)
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "signed_in" and c.get("/api/me/security").status_code == 200
    # The same code can't be used again (replay) and the step is spent.
    c.cookies.clear()
    sign_in(c)
    again = c.post("/api/auth/mfa/verify", json={"code": _code(secret, 1)}, headers=ORIGIN)
    assert again.status_code == 401


def test_recovery_code_works_once(hub):
    user = person(hub)
    c = client(hub)
    sign_in(c)
    _, codes = _enroll(c)
    for attempt in range(2):
        c.cookies.clear()
        sign_in(c)
        r = c.post("/api/auth/mfa/verify", json={"recovery_code": codes[0].lower()}, headers=ORIGIN)
        if attempt == 0:
            assert r.status_code == 200 and r.json()["recovery_codes_left"] == 9
        else:
            assert r.status_code == 401
    assert mfa.status(hub, user["id"])["recovery_codes_left"] == 9


def test_five_wrong_codes_end_the_step(hub):
    person(hub)
    c = client(hub)
    sign_in(c)
    secret, _ = _enroll(c)
    c.cookies.clear()
    sign_in(c)
    for _ in range(4):
        assert c.post("/api/auth/mfa/verify", json={"code": "111111"}, headers=ORIGIN).status_code == 401
    assert c.post("/api/auth/mfa/verify", json={"code": "111111"}, headers=ORIGIN).status_code == 410
    late = c.post("/api/auth/mfa/verify", json={"code": _code(secret, 1)}, headers=ORIGIN)
    assert late.status_code == 410  # sign in again for a new step


def test_a_known_password_does_not_reset_the_code_limit(hub, monkeypatch):
    # Someone with the password signs in again after every few wrong codes:
    # the account's counter still reaches the limit and locks.
    monkeypatch.setenv("HUBZOID_AUTH_MAX_FAILURES", "6")
    person(hub)
    c = client(hub)
    sign_in(c)
    secret, _ = _enroll(c)
    statuses = []
    for _ in range(4):
        c.cookies.clear()
        statuses.append(sign_in(c).status_code)
        for _ in range(2):
            statuses.append(c.post("/api/auth/mfa/verify", json={"code": "111111"},
                                   headers=ORIGIN).status_code)
    assert 429 in statuses, statuses
    assert sign_in(c).status_code == 429


def test_completed_two_step_sign_ins_never_add_up(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_AUTH_MAX_FAILURES", "3")
    person(hub)
    c = client(hub)
    sign_in(c)
    _secret, codes = _enroll(c)
    for i in range(5):
        c.cookies.clear()
        assert sign_in(c).json()["state"] == "mfa_required", i
        ok = c.post("/api/auth/mfa/verify", json={"recovery_code": codes[i]}, headers=ORIGIN)
        assert ok.status_code == 200, (i, ok.text)


def test_admins_must_set_up_at_sign_in_without_being_locked_out(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_MFA_REQUIRED", "admins")
    person(hub, email="boss@example.com", role="admin")
    person(hub, email="ana@example.com")
    c = client(hub)
    assert sign_in(c, email="ana@example.com").json()["state"] == "signed_in"
    c.cookies.clear()
    step = sign_in(c, email="boss@example.com")
    assert step.json()["state"] == "mfa_setup"
    secret = step.json()["setup"]["key"].replace(" ", "")
    # A reload shows the same set-up again.
    assert c.get("/api/auth/mfa/setup").json()["setup"]["key"].replace(" ", "") == secret
    done = c.post("/api/auth/mfa/setup", json={"code": _code(secret)}, headers=ORIGIN)
    assert done.status_code == 200 and done.json()["state"] == "signed_in"
    assert len(done.json()["recovery_codes"]) == 10
    # Required: it can't be turned off, only replaced.
    off = c.delete("/api/me/mfa", headers=ORIGIN)
    assert off.status_code == 409 and off.json()["detail"]["code"] == "mfa_required"


def test_sensitive_changes_need_a_recent_sign_in(hub, monkeypatch):
    person(hub)
    c = client(hub)
    sign_in(c)
    clock = [time.time() + sessions.RECENT_SECONDS + 5]
    monkeypatch.setattr(sessions.time, "time", lambda: clock[0])
    stale = c.post("/api/me/mfa/enroll", headers=ORIGIN)
    assert stale.status_code == 403 and stale.json()["detail"]["code"] == "reauth_required"
    assert stale.json()["detail"]["reauth"]["password"] is True
    assert c.post("/api/auth/reauth", json={"password": "wrong"}, headers=ORIGIN).status_code == 401
    assert c.post("/api/auth/reauth", json={"password": PASSWORD}, headers=ORIGIN).status_code == 200
    assert c.post("/api/me/mfa/enroll", headers=ORIGIN).status_code == 200


def test_devices_list_and_sign_out_another(hub):
    person(hub)
    phone, laptop = client(hub), client(hub)
    sign_in(phone)
    sign_in(laptop)
    listed = laptop.get("/api/me/security").json()["sessions"]
    assert len(listed) == 2 and sum(s["current"] for s in listed) == 1
    other = next(s for s in listed if not s["current"])
    assert laptop.delete(f"/api/me/sessions/{other['id']}", headers=ORIGIN).status_code == 204
    assert phone.get("/api/me/security").status_code == 401
    assert laptop.get("/api/me/security").status_code == 200
    sign_in(phone)
    assert laptop.post("/api/me/sessions/revoke-others", headers=ORIGIN).json()["ended"] == 1
    assert phone.get("/api/me/security").status_code == 401


def test_admin_reset_ends_the_factor_and_every_credential(hub):
    boss = person(hub, email="boss@example.com", role="admin")
    ana = person(hub)
    admin, member = client(hub), client(hub)
    sign_in(member)
    _enroll(member)
    sign_in(admin, email="boss@example.com")
    r = admin.post(f"/api/people/{ana['id']}/reset-mfa", headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert not mfa.enrolled(hub, ana["id"])
    assert member.get("/api/me/security").status_code == 401  # signed out
    assert users.epoch(hub, ana["id"]) == 1
    assert admin.post(f"/api/people/{boss['id']}/reset-mfa", headers=ORIGIN).status_code == 409


def test_password_reset_keeps_mfa_and_moves_the_counter(hub):
    ana = person(hub)
    c = client(hub)
    sign_in(c)
    _enroll(c)
    users.set_password(hub, ana["id"], "a brand new password")
    assert mfa.enrolled(hub, ana["id"]) and users.epoch(hub, ana["id"]) == 1
    c.cookies.clear()
    assert sign_in(c, password="a brand new password").json()["state"] == "mfa_required"


def test_policy_changes_are_admin_only(hub):
    person(hub, email="boss@example.com", role="admin")
    person(hub)
    member, admin = client(hub), client(hub)
    sign_in(member)
    sign_in(admin, email="boss@example.com")
    assert member.put("/api/auth/mfa/policy", json={"policy": "all"}, headers=ORIGIN).status_code == 403
    assert admin.put("/api/auth/mfa/policy", json={"policy": "all"}, headers=ORIGIN).json()["policy"] == "all"
    assert auth_settings.mfa_policy(hub) == "all"
    assert admin.put("/api/auth/mfa/policy", json={"policy": "bogus"}, headers=ORIGIN).status_code == 422
