"""Each conversation's files stay its own (review finding: conversation ids
aliasing another owner's files).

The per-chat folder ``.hubzoid/chats/<key>/`` is shared by every surface: the
web app, Open WebUI chats of Open WebUI mode, Slack, Telegram and WhatsApp. A
conversation started in the web app keeps its files in ``web-<id>`` (its chat
scope too), so a browser-chosen id never names another surface's folder, and
ids are unique ignoring case, so two conversations never share a folder on a
case-insensitive file system. A conversation imported from Open WebUI keeps
the folder ``hubzoid migrate openwebui`` copied its files to: its own id.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from hubzoid import memory, uploads
from hubzoid.chat import store as chat_store
from tests.chat_helpers import as_people, build_app, chat_body, events, make_hub, reply_text, who

OWUI_ID = "3f2a8b1c-0d4e-4f5a-9b6c-7d8e9f0a1b2c"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def app(hub, monkeypatch):
    as_people(monkeypatch)
    return build_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def agent(app):
    return app.state.chat.model_label


def _create(client, agent, conv_id, person):
    return client.post("/api/conversations", headers=who(person), json={"agent": agent, "id": conv_id})


def _upload(client, conv_id, name, payload, person):
    return client.post(f"/api/conversations/{conv_id}/files", headers=who(person),
                       files={"file": (name, payload, "text/plain")})


def _artifact_link(client, agent, conv_id, person, n=1):
    r = client.post("/api/chat", headers=who(person), json=chat_body(
        conv_id, "make an artifact", agent=agent, message_id=f"m_user{conv_id[-6:]}{n}",
        assistant_id=f"m_asst{conv_id[-6:]}{n}"))
    assert r.status_code == 200, r.text
    return urlsplit(re.search(r"\((http[^)]+scripted-report\.md\?[^)]+)\)",
                              reply_text(events(r.text))).group(1))


def test_a_web_conversation_keeps_its_files_in_its_own_folder(hub, client, agent):
    assert _create(client, agent, "c_folder0001", "ana").status_code == 201
    assert _upload(client, "c_folder0001", "notes.txt", b"hello", "ana").status_code == 201
    folder = hub / memory.CHATS_DIRNAME / "web-c_folder0001"
    assert (folder / "uploads" / "notes.txt").read_bytes() == b"hello"
    link = _artifact_link(client, agent, "c_folder0001", "ana")
    # the run's chat scope is the same folder, so the agent's files land there too
    assert (folder / "artifacts" / "scripted-report.md").is_file()
    assert link.path == "/artifacts/web-c_folder0001/scripted-report.md"
    assert client.get(link.path, headers={"X-Test-User": "ana"}).status_code == 200
    assert client.get(link.path, headers={"X-Test-User": "ben"}).status_code == 401
    assert client.get(f"{link.path}?{link.query}").status_code == 200
    assert not (hub / memory.CHATS_DIRNAME / "c_folder0001").exists()
    assert client.delete("/api/conversations/c_folder0001", headers=who("ana")).status_code == 204
    assert not folder.exists()


def test_a_case_variant_id_is_refused_and_never_opens_the_owners_files(hub, client, agent):
    assert _create(client, agent, "c_Victim0001", "ana").status_code == 201
    assert _upload(client, "c_Victim0001", "secret.txt", b"ana's secret", "ana").status_code == 201
    _artifact_link(client, agent, "c_Victim0001", "ana")
    for variant in ("c_victim0001", "C_VICTIM0001"):
        r = _create(client, agent, variant, "ben")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "id_conflict", variant
        r = client.post("/api/chat", headers=who("ben"), json=chat_body(
            variant, "hi", agent=agent, message_id="m_benuser01", assistant_id="m_benasst01"))
        assert r.status_code == 409 and r.json()["detail"]["code"] == "id_conflict", variant
        assert client.get(f"/api/conversations/{variant}/files/secret.txt",
                          headers=who("ben")).status_code == 404
        assert client.get(f"/artifacts/web-{variant}/scripted-report.md",
                          headers={"X-Test-User": "ben"}).status_code == 401
    folder = hub / memory.CHATS_DIRNAME / "web-c_Victim0001"
    assert (folder / "uploads" / "secret.txt").read_bytes() == b"ana's secret"
    assert client.get("/api/conversations/c_Victim0001/files/secret.txt",
                      headers=who("ana")).content == b"ana's secret"


@pytest.mark.parametrize("surface_chat", ["whatsapp-15551234567", "telegram-987654321", OWUI_ID])
def test_another_surfaces_chat_id_never_opens_its_files(hub, client, agent, surface_chat):
    """What the WhatsApp or Telegram channel, or Open WebUI before the move to
    the web app, stored for someone's chat stays out of reach of a web
    conversation that takes the same id."""
    up = memory.chat_upload_dir(hub, surface_chat)
    uploads.write_with_meta(up, "passport.txt", b"private", mime="text/plain")
    (memory.chat_artifact_dir(hub, surface_chat) / "summary.txt").write_text("private summary")
    assert _create(client, agent, surface_chat, "ben").status_code == 201
    assert client.get(f"/api/conversations/{surface_chat}/files/passport.txt",
                      headers=who("ben")).status_code == 404
    assert client.get(f"/artifacts/{surface_chat}/summary.txt",
                      headers={"X-Test-User": "ben"}).status_code == 401
    # attaching the other chat's file is refused, and the agent's turn has its own scope
    r = client.post("/api/chat", headers=who("ben"), json=chat_body(
        surface_chat, "read it", agent=agent, message_id="m_benuser01", assistant_id="m_benasst01",
        files=("passport.txt",)))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "file_not_found"
    link = _artifact_link(client, agent, surface_chat, "ben")
    assert link.path == f"/artifacts/web-{surface_chat}/scripted-report.md"
    assert not (memory.chat_root(hub, surface_chat) / "artifacts" / "scripted-report.md").exists()
    # deleting the web conversation leaves the other chat's files alone
    assert client.delete(f"/api/conversations/{surface_chat}", headers=who("ben")).status_code == 204
    assert (up / "passport.txt").read_bytes() == b"private"
    assert (memory.chat_root(hub, surface_chat) / "artifacts" / "summary.txt").is_file()


def test_a_migrated_conversation_keeps_its_open_webui_folder(hub, app, client, agent):
    store = app.state.chat.store
    store.create_conversation(conv_id=OWUI_ID, owner_id="u_ana", owner_email="ana@example.org",
                              hub="chat-hub", agent=agent, title="Old chat", title_source="migrated",
                              source="migrated")
    up = memory.chat_upload_dir(hub, OWUI_ID)
    uploads.write_with_meta(up, "old.txt", b"from open webui", mime="text/plain")
    (memory.chat_artifact_dir(hub, OWUI_ID) / "made.txt").write_text("an old artifact")
    assert client.get(f"/api/conversations/{OWUI_ID}/files/old.txt",
                      headers=who("ana")).content == b"from open webui"
    assert client.get(f"/api/conversations/{OWUI_ID}/files/old.txt", headers=who("ben")).status_code == 404
    # its 1.0.x download links open with the owner's session
    assert client.get(f"/artifacts/{OWUI_ID}/made.txt", headers={"X-Test-User": "ana"}).status_code == 200
    assert client.get(f"/artifacts/{OWUI_ID}/made.txt", headers={"X-Test-User": "ben"}).status_code == 401
    # continuing it: the old file can be attached, new files land in the same folder
    seen = []
    runtime = app.state.chat.runtime
    original = runtime.stream_events
    runtime.stream_events = lambda prompt: (seen.append(prompt), original(prompt))[1]
    r = client.post("/api/chat", headers=who("ana"), json=chat_body(
        OWUI_ID, "and now?", agent=None, message_id="m_anauser01", assistant_id="m_anaasst01",
        files=("old.txt",)))
    assert r.status_code == 200
    assert "[User attached file: old.txt (15 bytes, text/plain)." in seen[-1]
    assert f".hubzoid/chats/{OWUI_ID}/uploads/old.txt]" in seen[-1]
    assert _upload(client, OWUI_ID, "new.txt", b"new", "ana").status_code == 201
    assert (up / "new.txt").read_bytes() == b"new"
    link = _artifact_link(client, agent, OWUI_ID, "ana", n=2)
    assert link.path == f"/artifacts/{OWUI_ID}/scripted-report.md"
    assert (memory.chat_root(hub, OWUI_ID) / "artifacts" / "scripted-report.md").is_file()
    assert not (hub / memory.CHATS_DIRNAME / f"web-{OWUI_ID}").exists()
    assert client.delete(f"/api/conversations/{OWUI_ID}", headers=who("ana")).status_code == 204
    assert not memory.chat_root(hub, OWUI_ID).exists()


def test_chat_keys_and_their_conversations(tmp_path):
    from sqlalchemy import create_engine

    from hubzoid import migrations

    engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    migrations.upgrade(engine, "operational")
    store = chat_store.ConversationStore(engine)
    web = store.create_conversation(conv_id="c_keys00001", owner_id="u_ana", owner_email="a@x.org",
                                    hub="h", agent="a")
    old = store.create_conversation(conv_id=OWUI_ID, owner_id="u_ana", owner_email="a@x.org",
                                    hub="h", agent="a", source="migrated")
    odd = store.create_conversation(conv_id="web-c_keys00001", owner_id="u_ben", owner_email="b@x.org",
                                    hub="h", agent="a")
    assert chat_store.chat_key(web) == "web-c_keys00001"
    assert chat_store.chat_key(old) == OWUI_ID
    assert chat_store.chat_key(odd) == "web-web-c_keys00001"
    for conv in (web, old, odd):
        key = chat_store.chat_key(conv)
        assert memory.sanitize_chat_id(key) == key     # the artifact route's own name for it
        assert store.conversation_for_chat_key(key)["id"] == conv["id"]
    for key in ("c_keys00001", f"web-{OWUI_ID}", "WEB-c_keys00001", "web-C_KEYS00001", "web-", "x"):
        assert store.conversation_for_chat_key(key) is None, key
    engine.dispose()


def test_ids_fit_the_folder_name_limit(client, agent):
    """``web-<id>`` must stay a whole folder name (``memory.sanitize_chat_id``
    keeps 64 characters), so new ids have at most 60."""
    assert _create(client, agent, "c" * 60, "ana").status_code == 201
    r = _create(client, agent, "c" * 61, "ana")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_id"
