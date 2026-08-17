"""One-time OWUI config reseed when OWUI_NATIVE_MCP first turns on.

Clears the stale (previously-ignored) `config` table so OWUI re-seeds from env,
runs exactly once (so admin-registered tool servers persist after), touches only
the config table (users/groups/etc. untouched), and is safe on a fresh dir.
"""
from __future__ import annotations

import sqlite3

from hubzoid import webui

MARKER = ".hubzoid-owui-native-mcp-seeded"


def _make_db(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(data_dir / "webui.db")
    con.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
    con.execute('CREATE TABLE "user" (id TEXT, email TEXT)')
    con.execute("INSERT INTO config VALUES ('user.permissions', '{\"tools\": false}')")
    con.execute("INSERT INTO config VALUES ('tool_server.connections', '[]')")
    con.execute('INSERT INTO "user" VALUES (\'u1\', \'a@x.org\')')
    con.commit()
    con.close()


def _config_count(data_dir):
    con = sqlite3.connect(data_dir / "webui.db")
    n = con.execute("SELECT count(*) FROM config").fetchone()[0]
    con.close()
    return n


def test_reseed_clears_config_but_not_users(tmp_path):
    dd = tmp_path / "data"
    _make_db(dd)
    webui._seed_owui_config_once(dd)
    assert _config_count(dd) == 0                      # stale config cleared
    con = sqlite3.connect(dd / "webui.db")
    assert con.execute('SELECT count(*) FROM "user"').fetchone()[0] == 1   # users kept
    con.close()
    assert (dd / MARKER).exists()


def test_reseed_is_one_time_and_preserves_admin_data(tmp_path):
    dd = tmp_path / "data"
    _make_db(dd)
    webui._seed_owui_config_once(dd)                   # clears + marks
    # Admin registers a tool server AFTER the reseed (persistence now on):
    con = sqlite3.connect(dd / "webui.db")
    con.execute("INSERT INTO config VALUES ('tool_server.connections', '[{\"id\":\"linear\"}]')")
    con.commit()
    con.close()
    webui._seed_owui_config_once(dd)                   # marker present -> no-op
    assert _config_count(dd) == 1                      # admin's server preserved


def test_reseed_fresh_dir_no_db(tmp_path):
    dd = tmp_path / "data"                             # no webui.db yet
    webui._seed_owui_config_once(dd)
    assert (dd / MARKER).exists()
    assert not (dd / "webui.db").exists()              # does not fabricate a DB


def test_reseed_retries_if_delete_fails(tmp_path):
    # A webui.db with no `config` table (unexpected shape): DELETE errors, so we
    # must NOT write the marker - leave it to retry next boot.
    dd = tmp_path / "data"
    dd.mkdir(parents=True)
    con = sqlite3.connect(dd / "webui.db")
    con.execute("CREATE TABLE other (x INT)")
    con.commit()
    con.close()
    webui._seed_owui_config_once(dd)
    assert not (dd / MARKER).exists()                  # not marked -> will retry
