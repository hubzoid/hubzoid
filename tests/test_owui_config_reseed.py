"""Hubzoid owns connection wiring, not saved integration or account settings."""
from __future__ import annotations
import json
import sqlite3
from hubzoid import webui


def test_owned_wiring_refresh_preserves_other_settings(tmp_path):
    with sqlite3.connect(tmp_path / "webui.db") as con:
        con.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
        original = {"openai.api_keys": ["old"], "openai.api_base_urls": ["http://old/v1"],
                    "tool_server.connections": [{"id": "example"}], "user.permissions": {"tools": False}}
        con.executemany("INSERT INTO config VALUES (?, ?)", [(k,json.dumps(v)) for k,v in original.items()])
    webui._seed_owui_config_once(tmp_path)
    webui._sync_owned_connections(tmp_path, {"OPENAI_API_KEYS": "fresh;other", "OPENAI_API_BASE_URLS": "http://one/v1;http://two/v1"})
    with sqlite3.connect(tmp_path / "webui.db") as con:
        values = {k:json.loads(v) for k,v in con.execute("SELECT key,value FROM config")}
    assert values["openai.api_keys"] == ["fresh", "other"]
    assert values["openai.api_base_urls"] == ["http://one/v1", "http://two/v1"]
    for k in ("tool_server.connections", "user.permissions"):
        assert values[k] == original[k]
    webui._sync_owned_connections(tmp_path, {"OPENAI_API_KEYS": "rotated"})
    with sqlite3.connect(tmp_path / "webui.db") as con:
        assert json.loads(con.execute("SELECT value FROM config WHERE key='openai.api_keys'").fetchone()[0]) == ["rotated"]


def test_fresh_database_is_not_fabricated(tmp_path):
    webui._sync_owned_connections(tmp_path, {"OPENAI_API_KEYS": "fresh"})
    assert not (tmp_path / "webui.db").exists()
