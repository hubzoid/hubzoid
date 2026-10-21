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
        # Workspace edits are hub changes: refused for a hosted hub.
        r = client.post("/api/hub-changes", headers=ORIGIN, json={
            "title": "Edit", "edits": [{"path": "AGENTS.md", "content": "changed"}]})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "hub_read_only"
        r = client.put("/api/hitl/config", headers=ORIGIN, json={"text": "version: 1\n", "expected_revision": "absent"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "hub_read_only"


def test_hub_changes_are_refused_for_a_hosted_hub(tmp_path, monkeypatch):
    from hubzoid.hub_changes import service

    monkeypatch.setenv(datadir.ENV, str(tmp_path / "data"))
    with pytest.raises(service.ChangeError) as caught:
        service.propose(tmp_path / "hub", title="t", edits=[], actor="a@example.org", via="api")
    assert caught.value.code == "hub_read_only"


def test_budgets_cards_and_station_patches_respect_a_hosted_hub(tmp_path, monkeypatch):
    from hubzoid import budgets
    from hubzoid.apps import cards
    from hubzoid.sandbox import service as sandbox_service
    from hubzoid.stations import service as stations
    from hubzoid.work_items.errors import WorkItemError

    hub, data = tmp_path / "hub", tmp_path / "data"
    hub.mkdir()
    monkeypatch.setenv(datadir.ENV, str(data))
    with pytest.raises(WorkItemError) as caught:                     # budgets.yaml is a hub file
        budgets.apply(hub, "version: 1\n", expected_revision="absent", actor="a@example.org")
    assert caught.value.code == "hub_read_only" and not (hub / budgets.FILE).exists()
    with pytest.raises(WorkItemError) as caught:                     # so is mcp-apps.yaml
        cards.apply(hub, expected=None, actor="a@example.org", apps_doc={"version": 1})
    assert caught.value.code == "hub_read_only"

    went = []
    monkeypatch.setattr(sandbox_service, "get", lambda hub_dir, sid: {"checkouts_json": [{"path": "repo"}]})
    monkeypatch.setattr(sandbox_service, "export_patches", lambda hub_dir, sid, out: went.append(out))
    stations._export(hub, "sbx-1", "item-1", "call-1")                # a station's patches
    assert went == [data / "output" / "work-items" / "item-1" / "call-1"]


def test_session_output_and_deleted_chats_live_in_the_data_folder(tmp_path, monkeypatch):
    from hubzoid import memory
    from hubzoid.chat import store

    hub, data = tmp_path / "hub", tmp_path / "data"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("A test agent.")
    monkeypatch.setenv(datadir.ENV, str(data))
    spill = memory.session_output_dir(hub, "s1") / "read-overflow.txt"
    spill.write_text("the full text")
    assert spill.is_relative_to(data.resolve())
    assert invoke(registry(hub)["read_file"], path="output/s1/read-overflow.txt") == "the full text"
    chat = memory.chat_root(hub, "c_gone")
    (chat / "uploads").mkdir(parents=True)
    (chat / "uploads" / "a.txt").write_text("x")
    store.remove_chat_files(hub, "c_gone")
    assert not chat.exists()


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


def test_a_gateway_refuses_a_data_folder(tmp_path, monkeypatch):
    from hubzoid import gateway

    def hub(name, port, data):
        h = tmp_path / name
        h.mkdir()
        (h / "AGENTS.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody")
        (h / ".env").write_text(f"BRIDGE_PORT={port}\nMODEL=claude-local\n{datadir.ENV}={data}\n")
        return h

    monkeypatch.delenv(datadir.ENV, raising=False)
    with pytest.raises(ValueError, match="A gateway keeps each hub's state"):  # set in one hub's .env
        gateway.plan([hub("a", 8001, ""), hub("b", 8002, tmp_path / "own")])
    assert datadir.ENV not in os.environ                                       # each hub's env is put back
    assert len(gateway.plan([tmp_path / "a", hub("c", 8003, "")]).backends) == 2
    d = hub("d", 8004, "")
    (d / ".env").write_text("BRIDGE_PORT=8004\nMODEL=claude-local\n")
    monkeypatch.setenv(datadir.ENV, str(tmp_path / "data"))                    # or the gateway's own
    with pytest.raises(ValueError, match="A gateway keeps each hub's state"):
        gateway.plan([d])


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
