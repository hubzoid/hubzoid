"""Download links the agent writes: in the web app the conversation's signed-in
owner downloads without a token (so an expired link keeps working for them),
and new signed links expire after 7 days unless HUBZOID_ARTIFACT_LINK_TTL says
otherwise. Open WebUI mode is unchanged."""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from hubzoid import _signing, memory
from tests.chat_helpers import ORIGIN, as_people, build_app, chat_body, events, make_hub, reply_text, who

ARTIFACT = "/artifacts/web-c_art000001/scripted-report.md"   # the folder of web conversation c_art000001


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


def _make_artifact(client, app, headers):
    r = client.post("/api/chat", headers=headers, json=chat_body(
        "c_art000001", "make an artifact", agent=app.state.chat.model_label,
        message_id="m_user00001", assistant_id="m_asst00001"))
    assert r.status_code == 200
    link = re.search(r"\((http[^)]+scripted-report\.md\?[^)]+)\)", reply_text(events(r.text))).group(1)
    return link


def test_signed_in_owner_downloads_without_a_token(hub, monkeypatch):
    as_people(monkeypatch)
    app = build_app()
    client = TestClient(app)
    link = _make_artifact(client, app, who("ana"))
    parts = urlsplit(link)
    assert parts.path == ARTIFACT
    # the signed link works for anyone who has it
    assert client.get(f"{parts.path}?{parts.query}").status_code == 200
    # the owner's session is enough
    r = client.get(ARTIFACT, headers={"X-Test-User": "ana"})
    assert r.status_code == 200 and "Scripted report" in r.text
    # someone else, or nobody, needs the link
    assert client.get(ARTIFACT, headers={"X-Test-User": "ben"}).status_code == 401
    assert client.get(ARTIFACT).status_code == 401


def test_web_app_links_expire_after_seven_days(hub, monkeypatch):
    as_people(monkeypatch)
    app = build_app()
    client = TestClient(app)
    link = _make_artifact(client, app, who("ana"))
    query = parse_qs(urlsplit(link).query)
    expires = int(query["e"][0])
    assert abs(expires - (time.time() + 7 * 24 * 3600)) < 120
    # an expired link still works for the owner, not for others
    past = int(time.time()) - 5
    token = _signing._mac("web-c_art000001", "scripted-report.md", past, hub)
    expired = f"{ARTIFACT}?t={token}&e={past}"
    assert client.get(expired, headers={"X-Test-User": "ben"}).status_code == 401
    assert client.get(expired, headers={"X-Test-User": "ana"}).status_code == 200


def test_link_lifetime_can_be_set(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_ARTIFACT_LINK_TTL", "0")
    assert "&e=" not in _signing.artifact_query("c1", "a.txt", hub_dir=hub)
    monkeypatch.setenv("HUBZOID_ARTIFACT_LINK_TTL", "60")
    expires = int(parse_qs(_signing.artifact_query("c1", "a.txt", hub_dir=hub))["e"][0])
    assert abs(expires - (time.time() + 60)) < 30
    monkeypatch.setenv("HUBZOID_ARTIFACT_LINK_TTL", "soon")   # not a number: the default
    expires = int(parse_qs(_signing.artifact_query("c1", "a.txt", hub_dir=hub))["e"][0])
    assert expires > time.time() + 6 * 24 * 3600


def test_local_mode_owner_downloads(hub):
    app = build_app()
    client = TestClient(app)
    _make_artifact(client, app, ORIGIN)
    assert client.get(ARTIFACT).status_code == 200


def test_another_hubs_conversation_does_not_open_this_hubs_files(hub, monkeypatch):
    as_people(monkeypatch)
    app = build_app()
    client = TestClient(app)
    app.state.chat.store.create_conversation(conv_id="c_art000002", owner_id="u_ana",
                                             owner_email="ana@example.org", hub="another-hub",
                                             agent="x")
    folder = memory.chat_artifact_dir(hub, "web-c_art000002")
    (folder / "left.txt").write_text("from an API call")
    assert client.get("/artifacts/web-c_art000002/left.txt",
                      headers={"X-Test-User": "ana"}).status_code == 401


def test_legacy_mode_does_not_accept_sessions(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    as_people(monkeypatch)
    app = build_app()
    assert not hasattr(app.state, "chat")          # no web app routes in Open WebUI mode
    client = TestClient(app)
    from hubzoid.chat import store as chat_store

    chat_store.for_hub(hub).create_conversation(conv_id="c_legacy001", owner_id="u_ana",
                                                owner_email="ana@example.org", hub="chat-hub",
                                                agent="x")
    (memory.chat_artifact_dir(hub, "c_legacy001") / "a.txt").write_text("legacy")
    assert client.get("/artifacts/c_legacy001/a.txt", headers={"X-Test-User": "ana"}).status_code == 401
    query = _signing.artifact_query("c_legacy001", "a.txt", hub_dir=hub)
    assert "&e=" not in query                      # legacy links never expire by default
    assert client.get(f"/artifacts/c_legacy001/a.txt?{query}").status_code == 200
