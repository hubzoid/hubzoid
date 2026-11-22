"""The web app's read-only access routes (hubzoid.access.access_routes): what
the signed-in person can use and who to ask, a check that fails closed
(503, never a guess at more access), who a run was for (only for that person
or an administrator), and the API reference for signed-in people."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hubzoid.access import access_routes
from tests.chat_helpers import ORIGIN, build_app, make_hub
from tests.test_cov_chat_engine_cli import send


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


def test_who_a_run_was_for_and_the_api_reference(client):
    r = client.get("/api/run-identity", params={"kind": "chat", "id": "no-such-message"})
    assert r.status_code == 200 and r.json()["identity"] is None and r.json()["summary"]

    agent = client.app.state.chat.model_label
    made = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent}).json()
    sent = send(client, made["conversation"]["id"], made["branch"], "hello")
    r = client.get("/api/run-identity", params={"kind": "chat", "id": sent["assistant_message_id"]})
    ident = r.json()["identity"]                          # recorded for the reply: its owner may see it
    assert r.status_code == 200 and ident["run_kind"] == "chat" and ident["hub"] == "chat-hub"
    assert ident["acts_as_email"] == ident["requested_by"] == "admin@localhost" and r.json()["summary"]

    ref = client.get("/api/openapi.json")
    assert ref.status_code == 200 and ref.json()["openapi"].startswith("3.") and "/api/me/access" in ref.json()["paths"]
