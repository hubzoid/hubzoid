"""Member features (HUB-77 voice) and devices (HUB-83): grants, input
preferences, shortcuts, the offline worker and install metadata."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    return TestClient(build_app())


def apply(hub, change):
    from hubzoid.chat import config as cfg

    svc = cfg.service_for(hub)
    active = svc.active()
    doc = active.document
    change(doc)
    __import__('tests.chat_helpers', fromlist=['x']).write_chat_config(svc.hub_dir, doc)


def test_voice_is_off_for_members_until_granted(client, hub, monkeypatch):
    from hubzoid.access import store_for

    as_people(monkeypatch)
    feats = client.get("/api/chat/capabilities", headers=who("ana")).json()["features"]
    assert feats == {"chat_dictation": False, "chat_read_aloud": False, "chat_web_search": False,
                     "chat_model_compare": False, "helper_followups": False, "web_search": False,
                     "feedback": True}   # ratings are collected by default (chat/config.yaml feedback.enabled)
    store_for(hub).grant("ana@example.org", hub.name, "chat_dictation", actor="t")
    feats = client.get("/api/chat/capabilities", headers=who("ana")).json()["features"]
    assert feats["chat_dictation"] is True and feats["chat_read_aloud"] is False
    store_for(hub).grant("ben@example.org", hub.name, "manage_access", actor="t")
    ben = client.get("/api/chat/capabilities", headers=who("ben")).json()["features"]
    assert all(ben[k] for k in ("chat_dictation", "chat_read_aloud", "chat_web_search", "chat_model_compare"))


def test_input_preferences_follow_allow_personal(client, hub, monkeypatch):
    as_people(monkeypatch)
    ok = client.put("/api/chat/preferences", headers=who("ana"),
                    json={"expected_revision": 0, "values": {"send_key": "mod_enter", "contrast": "more"}})
    assert ok.status_code == 200, ok.text
    bad = client.put("/api/chat/preferences", headers=who("ana"),
                     json={"expected_revision": 1, "values": {"send_key": "shift_enter"}})
    assert bad.status_code == 422
    apply(hub, lambda d: d["devices"]["allow_personal"].update(contrast=False))
    locked = client.put("/api/chat/preferences", headers=who("ana"),
                        json={"expected_revision": 1, "values": {"contrast": "more"}})
    assert locked.status_code == 422 and locked.json()["detail"]["code"] == "policy_locked"


def test_shortcuts_reject_reserved_conflicting_and_single_keys(client, monkeypatch):
    as_people(monkeypatch)
    v = client.post("/api/chat/shortcuts/validate", headers=who("ana"), json={"shortcuts": {"chat.new": "$mod+W"}})
    assert v.json()["error"]["code"] == "reserved_chord"
    v = client.post("/api/chat/shortcuts/validate", headers=who("ana"), json={"shortcuts": {"chat.new": "k"}})
    assert not v.json()["valid"]
    clash = client.put("/api/chat/preferences", headers=who("ana"),
                       json={"expected_revision": 0, "values": {"shortcuts": {"chat.new": "$mod+K"}}})
    assert clash.status_code == 422 and clash.json()["detail"]["code"] == "shortcut_conflict"
    ok = client.put("/api/chat/preferences", headers=who("ana"),
                    json={"expected_revision": 0, "values": {"shortcuts": {"chat.new": "$mod+shift+j",
                                                                            "chat.stop": None}}})
    assert ok.status_code == 200
    eff = client.get("/api/chat/shortcuts", headers=who("ana")).json()["actions"]
    assert eff["chat.new"] == {"label": "New chat", "chord": "$mod+Shift+J", "source": "account"}
    assert eff["chat.stop"]["chord"] is None


def test_offline_worker_never_caches_api_and_can_remove_itself(client, hub):
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert 'url.pathname.startsWith("/api/")' in sw.text and "hz:logout" in sw.text
    assert client.get("/offline-shell").status_code == 200
    apply(hub, lambda d: d["devices"]["offline"].update(shell_cache=False))
    gone = client.get("/sw.js").text
    assert "unregister()" in gone and "addAll" not in gone
    probe = client.get("/api/chat/connection")
    assert probe.headers["cache-control"] == "no-store" and probe.json()["ok"]


def test_manifest_follows_devices_install(client, hub):
    m = client.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and m["share_target"]["params"]["text"] == "share_text"
    assert "orientation" not in m
    apply(hub, lambda d: d["devices"]["install"].update(display="minimal-ui", start_path="/new"))
    m = client.get("/manifest.webmanifest").json()
    assert m["display"] == "minimal-ui" and m["start_url"] == "/new"
    apply(hub, lambda d: d["devices"]["install"].update(enabled=False))
    assert client.get("/manifest.webmanifest").status_code == 404
