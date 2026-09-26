"""The connection pages under /portal/connect: session binding, same-origin
start, the Open WebUI authorize redirect and cookie, verification that never
reads callback parameters, and the success, failure, cancel and expiry pages.

The Open WebUI session is stubbed by a header in most tests. One test runs the
real server-side session check (`access.session.verified_email`) against a
mocked Open WebUI `/api/v1/auths/`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid import _request_ctx, connect_journey
from hubzoid.access import Identity, identity_scope
from hubzoid.connect_journey import store
from tests import connect_helpers as h

ALICE, BOB = "alice@example.org", "bob@example.org"
SECRET = "web-secret"


class _Hub(type(Path())):
    """A hub path that can also carry the test's fakes."""


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = _Hub(tmp_path / "mailhub")
    hub.mkdir()
    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[("ua", ALICE), ("ub", BOB)], secret=SECRET, servers=[
        {"id": "gmail", "name": "Gmail", "url": "https://gmail-mcp.example.org/mcp"}])
    h.owui_env(monkeypatch, db, SECRET)
    h.isolated_store(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_CONNECT_JOURNEY", "true")
    monkeypatch.setenv("WEBUI_URL", "https://hub.example.org")
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "owui,web,api,mcp,whatsapp")
    hub.db = db
    return hub


@pytest.fixture
def client(hub):
    app = FastAPI()
    app.include_router(connect_journey.build_router(
        hub, session_email=lambda request: request.headers.get("x-test-session", "")))
    c = TestClient(app, base_url="https://hub.example.org", follow_redirects=False)
    return c


def _link(hub, email=ALICE, surface="whatsapp", reconnect=False):
    with identity_scope(Identity.make(user=email, groups=["connector_gmail"], surface=surface)), \
            _request_ctx.chat_scope(f"{surface}-919800000001"):
        r = connect_journey.start(hub, app="gmail", reconnect=reconnect)
    return r["id"]


def _as(email):
    return {"x-test-session": email}


_SAME = {"origin": "https://hub.example.org"}


def _start(client, jid, email=ALICE):
    return client.post(f"/portal/connect/{jid}/start", headers={**_as(email), **_SAME})


def _audit(hub):
    from sqlalchemy import text

    import hubzoid.db as db
    with db.operational_engine(hub).connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT subject, tool, decision, reason FROM hz_access_decisions ORDER BY ts"))]


# ---------------------------------------------------------------------------
# The link page
# ---------------------------------------------------------------------------
def test_unknown_and_malformed_links_are_404(client):
    assert client.get("/portal/connect/" + "A" * 32).status_code == 404
    assert client.get("/portal/connect/nope").status_code == 404
    assert client.get("/portal/connect/" + "A" * 32 + "/status").status_code == 404


def test_a_signed_out_opener_signs_in_and_comes_back(client, hub):
    jid = _link(hub)
    r = client.get(f"/portal/connect/{jid}")
    assert r.status_code == 302
    assert r.headers["location"] == f"/auth?redirect=/portal/connect/{jid}"
    assert ALICE not in r.text + r.headers["location"]  # never disclosed to an anonymous opener
    done = client.get(f"/portal/connect/{jid}/done")
    assert done.headers["location"] == f"/auth?redirect=/portal/connect/{jid}/done"
    # A post cannot be redirected: it shows the page, whose link also comes back.
    r = client.post(f"/portal/connect/{jid}/start", headers=_SAME)
    assert r.status_code == 401 and f"/auth?redirect=/portal/connect/{jid}" in r.text
    assert "come back here after signing in" in r.text
    assert store.get(hub, jid)["status"] == "pending"


def test_an_unknown_link_never_redirects_to_sign_in(client):
    r = client.get("/portal/connect/" + "Z" * 32)
    assert r.status_code == 404 and "location" not in r.headers


def test_another_account_is_refused_and_audited(client, hub):
    jid = _link(hub)
    for path, method in ((f"/portal/connect/{jid}", "get"), (f"/portal/connect/{jid}/done", "get"),
                         (f"/portal/connect/{jid}/start", "post")):
        r = getattr(client, method)(path, headers={**_as(BOB), **_SAME})
        assert r.status_code == 403, path
        assert "another account" in r.text and ALICE not in r.text
    assert client.get(f"/portal/connect/{jid}/status", headers=_as(BOB)).status_code == 403
    assert (BOB, "connect:gmail", "deny", "wrong-account") in _audit(hub)
    assert store.get(hub, jid)["status"] == "pending"  # the owner can still use it


def test_the_owner_sees_the_connect_page_with_safe_headers(client, hub):
    jid = _link(hub)
    r = client.get(f"/portal/connect/{jid}", headers=_as(ALICE))
    assert r.status_code == 200
    assert "Connect Gmail" in r.text and ALICE in r.text
    assert f'action="/portal/connect/{jid}/start"' in r.text
    assert r.headers["cache-control"] == "no-store"
    # same-origin, not no-referrer: with no-referrer the browser posts the
    # Continue/Cancel forms with "Origin: null", which the server must refuse.
    assert r.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
def test_start_requires_the_same_origin(client, hub):
    jid = _link(hub)
    assert client.post(f"/portal/connect/{jid}/start", headers=_as(ALICE)).status_code == 403
    r = client.post(f"/portal/connect/{jid}/start",
                    headers={**_as(ALICE), "origin": "https://evil.example.com"})
    assert r.status_code == 403
    for bad in ({"origin": "null"}, {"referer": "https://evil.example.com/x"},
                {"origin": "https://hub.example.org.evil.com"}):
        r = client.post(f"/portal/connect/{jid}/start", headers={**_as(ALICE), **bad})
        assert r.status_code == 403, bad
        r = client.post(f"/portal/connect/{jid}/cancel", headers={**_as(ALICE), **bad})
        assert r.status_code == 403, bad
    assert store.get(hub, jid)["status"] == "pending"


def test_a_same_origin_referer_alone_is_accepted(client, hub):
    """What a browser sends for the form post under Referrer-Policy: same-origin
    when it omits Origin: a same-origin Referer."""
    jid = _link(hub)
    r = client.post(f"/portal/connect/{jid}/start",
                    headers={**_as(ALICE), "referer": f"https://hub.example.org/portal/connect/{jid}"})
    assert r.status_code == 303


def test_start_sends_the_browser_to_open_webui_with_the_journey_cookie(client, hub):
    jid = _link(hub)
    r = _start(client, jid)
    assert r.status_code == 303
    assert r.headers["location"] == "/oauth/clients/mcp:gmail/authorize"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"hz_connect={jid};")
    for part in ("HttpOnly", "Path=/", "SameSite=lax", "Max-Age=600", "Secure"):
        assert part in cookie
    j = store.get(hub, jid)
    assert j["status"] == "started" and j["started"]
    assert store.ID_RE.match(jid)  # matches the edge's cookie pattern


def test_start_rechecks_a_revoked_grant_on_a_managed_hub(client, hub):
    import hubzoid.access as access

    gs = access.store_for(hub)
    gs.set_authoritative(True, hub=hub.name)
    gs.grant(ALICE, hub.name, "connector_gmail")
    jid = _link(hub)
    gs.revoke(ALICE, hub.name, "connector_gmail")
    r = _start(client, jid)
    assert r.status_code == 403 and "no longer have permission" in r.text
    assert store.get(hub, jid)["status"] == "pending"


def test_start_refuses_a_server_the_admin_removed(client, hub, tmp_path, monkeypatch):
    jid = _link(hub)
    db = tmp_path / "webui-empty.db"
    h.seed_owui(db, users=[("ua", ALICE)], secret=SECRET, servers=[])
    h.owui_env(monkeypatch, db, SECRET)
    r = _start(client, jid)
    assert r.status_code == 410 and "no longer available" in r.text


# ---------------------------------------------------------------------------
# Done and status: verified with the provider only
# ---------------------------------------------------------------------------
def test_done_ignores_callback_parameters_and_verifies_with_open_webui(client, hub):
    jid = _link(hub)
    _start(client, jid)
    # A forged or replayed "success" callback proves nothing.
    r = client.get(f"/portal/connect/{jid}/done?status=success&code=x&state=y", headers=_as(ALICE))
    assert r.status_code == 202 and "Finishing up" in r.text
    assert "hz_connect=" in r.headers["set-cookie"]  # the cookie is cleared
    assert client.get(f"/portal/connect/{jid}/status", headers=_as(ALICE)).json()["state"] == "started"
    # Open WebUI's callback stores the session.
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-a")
    assert client.get(f"/portal/connect/{jid}/status", headers=_as(ALICE)).json()["state"] == "connected"
    r = client.get(f"/portal/connect/{jid}/done", headers=_as(ALICE))
    assert r.status_code == 200 and "Gmail is connected" in r.text and ALICE in r.text
    assert "WhatsApp" in r.text
    assert "AT-a" not in r.text


def test_the_finishing_page_polls_status_under_a_nonce(client, hub):
    jid = _link(hub)
    _start(client, jid)
    r = client.get(f"/portal/connect/{jid}/done", headers=_as(ALICE))
    csp = r.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in r.text and f"/portal/connect/{jid}/status" in r.text


def test_a_failed_connection_shows_the_failure_page(client, hub):
    jid = _link(hub)
    _start(client, jid)
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT",
              created_at=time.time() + 1, expires_in=-120)
    r = client.get(f"/portal/connect/{jid}/done", headers=_as(ALICE))
    assert r.status_code == 200 and "was not connected" in r.text and "new link" in r.text


def test_cancel_then_start_shows_cancelled(client, hub):
    jid = _link(hub)
    assert client.post(f"/portal/connect/{jid}/cancel", headers=_as(ALICE)).status_code == 403
    r = client.post(f"/portal/connect/{jid}/cancel", headers={**_as(ALICE), **_SAME})
    assert "cancelled" in r.text.lower()
    assert store.get(hub, jid)["status"] == "cancelled"
    r = _start(client, jid)
    assert r.status_code == 200 and "cancelled" in r.text.lower()


def test_expired_and_superseded_links_are_gone(client, hub):
    jid = _link(hub)
    j = store.get(hub, jid)
    connect_journey.finalize(hub, j, now=j["expires"] + 1)
    r = client.get(f"/portal/connect/{jid}")
    assert r.status_code == 410 and "expired" in r.text
    old = _link(hub, reconnect=True)
    _link(hub, reconnect=True)
    r = client.get(f"/portal/connect/{old}", headers=_as(ALICE))
    assert r.status_code == 410 and "newer" in r.text
    assert _start(client, old).status_code == 410


def test_reconnect_ends_with_one_session(client, hub):
    import sqlite3
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-old",
              created_at=time.time() - 3600, expires_in=7200)
    jid = _link(hub, reconnect=True)
    _start(client, jid)
    assert client.get(f"/portal/connect/{jid}/status", headers=_as(ALICE)).json()["state"] == "started"
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-new")
    assert client.get(f"/portal/connect/{jid}/status", headers=_as(ALICE)).json()["state"] == "connected"
    count = sqlite3.connect(hub.db).execute(
        "SELECT count(*) FROM oauth_session WHERE user_id='ua'").fetchone()[0]
    assert count == 1


def test_web_chat_journey_says_return_to_the_chat(client, hub):
    jid = _link(hub, surface="owui")
    _start(client, jid)
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-a")
    r = client.get(f"/portal/connect/{jid}/done", headers=_as(ALICE))
    assert "return to the chat" in r.text and "WhatsApp" not in r.text


# ---------------------------------------------------------------------------
# The real session check
# ---------------------------------------------------------------------------
def test_real_open_webui_session_check(hub, monkeypatch):
    import httpx

    monkeypatch.setenv("OWUI_INTERNAL_URL", "http://owui.internal")
    users = {"tok-alice": {"id": "ua", "email": "Alice@Example.org", "role": "user", "name": "A"},
             "tok-bob": {"id": "ub", "email": BOB, "role": "user", "name": "B"},
             "tok-pending": {"id": "up", "email": ALICE, "role": "pending", "name": "P"}}
    seen = []

    def fake_get(url, headers=None, timeout=None):  # noqa: ARG001
        seen.append(url)
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        user = users.get(token)
        return httpx.Response(200 if user else 401, json=user or {"detail": "no"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    app = FastAPI()
    app.include_router(connect_journey.build_router(hub))
    c = TestClient(app, base_url="https://hub.example.org", follow_redirects=False)
    jid = _link(hub)
    c.cookies.set("token", "tok-bob")
    assert c.get(f"/portal/connect/{jid}").status_code == 403
    signin = f"/auth?redirect=/portal/connect/{jid}"
    c.cookies.set("token", "tok-pending")
    assert c.get(f"/portal/connect/{jid}").headers["location"] == signin
    c.cookies.set("token", "forged")
    assert c.get(f"/portal/connect/{jid}").headers["location"] == signin
    c.cookies.set("token", "tok-alice")
    assert c.get(f"/portal/connect/{jid}").status_code == 200
    assert all(u == "http://owui.internal/api/v1/auths/" for u in seen)
    # A client-sent identity header is never trusted.
    c.cookies.clear()
    r = c.get(f"/portal/connect/{jid}", headers={"X-OpenWebUI-User-Email": ALICE})
    assert r.status_code == 302 and r.headers["location"] == signin


def test_owui_outage_is_a_retry_not_a_pass(hub, monkeypatch):
    import httpx

    monkeypatch.setenv("OWUI_INTERNAL_URL", "http://owui.internal")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(
        502, request=httpx.Request("GET", "http://owui.internal/api/v1/auths/")))
    app = FastAPI()
    app.include_router(connect_journey.build_router(hub))
    c = TestClient(app, base_url="https://hub.example.org")
    jid = _link(hub)
    c.cookies.set("token", "tok")
    r = c.get(f"/portal/connect/{jid}")
    assert r.status_code == 503
    assert json.loads(c.get(f"/portal/connect/{jid}/status").text)["state"] == "unavailable"

