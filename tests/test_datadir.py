"""HUBZOID_DATA_DIR: a hosted hub's state lives in one data folder, and its own
files, baked into the image from Git, are only read."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hubzoid import datadir
from tests.chat_helpers import ORIGIN, build_app, chat_body, make_hub
from tests.test_chat_file_service import upload
from tests.test_file_read_security import CANARY, invoke, registry


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def test_paths_follow_the_data_folder_only_when_it_is_set(tmp_path):
    hub, data = tmp_path / "hub", tmp_path / "data"
    assert datadir.state(hub, env={}) == hub / ".hubzoid" and not datadir.hub_read_only(hub, env={})
    env = {datadir.ENV: str(data)}
    assert datadir.state(hub, env) == data.resolve() / ".hubzoid"
    assert datadir.resolve(hub, ".hubzoid/schedule/daily", env) == data.resolve() / ".hubzoid/schedule/daily"
    assert datadir.resolve(hub, "output/s1/x.txt", env) == data.resolve() / "output/s1/x.txt"
    assert datadir.resolve(hub, "knowledge/faq.md", env) == hub / "knowledge/faq.md"
    assert datadir.roots(hub, env) == [hub.resolve(), data.resolve()] and datadir.hub_read_only(hub, env)
    assert not datadir.hub_read_only(hub, {datadir.ENV: str(hub)})      # the hub is its own data folder


def test_a_chat_turn_and_an_upload_write_nothing_under_the_hub(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    hub = make_hub(tmp_path, monkeypatch, HUBZOID_DATA_DIR=str(data))
    before = files_under(hub)
    with TestClient(build_app()) as client:
        agent = client.app.state.chat.model_label
        r = client.post("/api/chat", headers=ORIGIN, json=chat_body(
            "c_datadir01", "hello", agent=agent, message_id="m_user00001", assistant_id="m_asst00001"))
        assert r.status_code == 200, r.text
        upload(client, b"name,qty\nwidget,3\n")
    assert files_under(hub) == before                                   # the hub folder is only read
    written = files_under(data)
    assert ".hubzoid/hub.db" in written and ".hubzoid/artifact_secret" not in before


def test_file_tools_read_this_chats_files_in_the_data_folder_and_nothing_private(tmp_path, monkeypatch):
    from hubzoid import _request_ctx, memory

    hub, data = tmp_path / "hub", tmp_path / "data"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("A test agent.")
    monkeypatch.setenv(datadir.ENV, str(data))
    with _request_ctx.chat_scope("hosted-chat"):
        uploads = memory.chat_upload_dir(hub, "hosted-chat")
        uploads.mkdir(parents=True, exist_ok=True)
        (uploads / "notes.txt").write_text("my uploaded notes")
        assert uploads.is_relative_to(data.resolve())
        other = memory.chat_upload_dir(hub, "other-chat")
        other.mkdir(parents=True, exist_ok=True)
        (other / "notes.txt").write_text(CANARY)
        (data / ".hubzoid" / "hub.db").write_text(CANARY)
        tools = registry(hub)
        assert invoke(tools["read_file"], path=str(uploads / "notes.txt")) == "my uploaded notes"
        assert "my uploaded notes" in invoke(tools["read_upload"], filename="notes.txt")
        for private in (other / "notes.txt", data / ".hubzoid" / "hub.db"):
            out = invoke(tools["read_file"], path=str(private))
            assert "refused" in out and CANARY not in out


def test_a_hosted_hubs_files_are_read_only_in_settings(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    make_hub(tmp_path, monkeypatch, HUBZOID_DATA_DIR=str(data))
    with TestClient(build_app()) as client:
        listed = client.get("/api/chat/authoring/files", headers=ORIGIN)
        assert listed.status_code == 200 and listed.json()["read_only"] is True
        r = client.post("/api/chat/authoring/drafts", headers=ORIGIN, json={"path": "AGENTS.md"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "hub_read_only"


def test_doctor_says_where_state_lives(tmp_path, monkeypatch):
    from hubzoid import doctor

    hub, data = tmp_path / "hub", tmp_path / "data"
    hub.mkdir()
    monkeypatch.delenv(datadir.ENV, raising=False)
    assert doctor._data_dir(hub) is None
    monkeypatch.setenv(datadir.ENV, str(data))
    assert doctor._data_dir(hub).status == "fail"                       # missing
    data.mkdir()
    assert doctor._data_dir(hub).status == "info"
    (hub / ".hubzoid").mkdir()
    (hub / ".hubzoid" / "hub.db").write_text("")
    row = doctor._data_dir(hub)
    assert row.status == "warn" and "older copy" in row.summary


def test_a_gateway_refuses_one_data_folder_for_several_hubs(tmp_path, monkeypatch):
    from hubzoid import gateway

    monkeypatch.setenv(datadir.ENV, str(tmp_path / "data"))
    with pytest.raises(ValueError, match="one hub's state"):
        gateway.plan([tmp_path / "a", tmp_path / "b"])


def test_a_backup_copies_the_data_folder(tmp_path, monkeypatch):
    from hubzoid import backup

    hub, data = tmp_path / "hub", tmp_path / "data"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("A test agent.")
    for name in (".hubzoid", "output"):
        (data / name).mkdir(parents=True)
    monkeypatch.setenv(datadir.ENV, str(data))
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    roots = {r.path for r in backup.plan(hub).roots if r.kind == "state"}
    assert roots == {data.resolve() / ".hubzoid", data.resolve() / "output"}
    assert not any(str(r).startswith(str(hub.resolve()) + os.sep) for r in roots)
