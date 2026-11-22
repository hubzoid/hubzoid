"""The web app's read-only access routes (hubzoid.access.access_routes): what
the signed-in person can use and who to ask, a check that fails closed
(503, never a guess at more access) and the API reference for signed-in
people. Who a run was for needs a finished reply, which can race without the
run status fix in #163: that test waits for it."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hubzoid.access import access_routes
from tests.chat_helpers import build_app, make_hub


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.delenv("HUBZOID_SUPPORT_CONTACT", raising=False)
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def test_my_access_and_who_to_ask(client, monkeypatch):
    body = client.get("/api/me/access").json()
    assert body["contact"] == "your administrator" and isinstance(body["features"], list)
    assert all({"key", "label", "on"} <= set(f) for f in body["features"])
    assert [a["hub"] for a in body["agents"]] and client.get("/api/me/access").headers["cache-control"] == "no-store"
    monkeypatch.setenv("HUBZOID_SUPPORT_CONTACT", "it-help@example.org")
    assert client.get("/api/me/access").json()["contact"] == "it-help@example.org"


def test_a_broken_access_check_fails_closed(client, monkeypatch):
    def broken(hub_dir, user):
        raise RuntimeError("store down")

    monkeypatch.setattr(access_routes, "my_access", broken)
    r = client.get("/api/me/access")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "access_unavailable"
