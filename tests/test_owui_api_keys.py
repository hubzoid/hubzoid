"""Resolver tests: OWUI per-user API key -> owning user's email.

Fail-closed in every branch: missing DB, unknown key, expired key, blank
token. A tiny sqlite file stands in for OWUI's database — same tables the
real lookup reads.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hubzoid.access import owui_api_keys


def _mk_db(path: Path, rows: list[tuple[str, str, object]]) -> Path:
    """rows = [(email, key, expires_at), ...]; one user per row."""
    con = sqlite3.connect(path)
    con.executescript(
        'CREATE TABLE "user"(id TEXT PRIMARY KEY, email TEXT);\n'
        'CREATE TABLE api_key(id TEXT, user_id TEXT, "key" TEXT, expires_at BIGINT);'
    )
    for i, (email, key, expires_at) in enumerate(rows):
        con.execute('INSERT INTO "user" VALUES (?, ?)', (f"u{i}", email))
        con.execute(
            "INSERT INTO api_key VALUES (?, ?, ?, ?)", (f"k{i}", f"u{i}", key, expires_at)
        )
    con.commit()
    con.close()
    return path


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A hub dir whose OWUI DB is a temp sqlite (via the env override)."""
    db = _mk_db(
        tmp_path / "webui.db",
        [
            ("alice@example.com", "sk-alice", None),
            ("bob@example.com", "sk-bob-expired", int(time.time()) - 60),
            ("carol@example.com", "sk-carol-ms", int((time.time() + 3600) * 1000)),
        ],
    )
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    return tmp_path


def test_valid_key_resolves_email(hub):
    assert owui_api_keys.resolve_email(hub, "sk-alice") == "alice@example.com"


def test_unknown_key_denied(hub):
    assert owui_api_keys.resolve_email(hub, "sk-nope") is None


def test_expired_key_denied(hub):
    assert owui_api_keys.resolve_email(hub, "sk-bob-expired") is None


def test_millisecond_expiry_in_future_allowed(hub):
    # OWUI stores epoch seconds; a ms-scale value must be read as ms, not as
    # a date in the year 58,000.
    assert owui_api_keys.resolve_email(hub, "sk-carol-ms") == "carol@example.com"


def test_blank_token_denied(hub):
    assert owui_api_keys.resolve_email(hub, "") is None
    assert owui_api_keys.resolve_email(hub, None) is None
    assert owui_api_keys.resolve_email(hub, "   ") is None


def test_missing_db_denied(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(tmp_path / "absent.db"))
    assert owui_api_keys.resolve_email(tmp_path, "sk-alice") is None


def test_garbage_db_denied(tmp_path, monkeypatch):
    bad = tmp_path / "webui.db"
    bad.write_text("this is not sqlite")
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(bad))
    assert owui_api_keys.resolve_email(tmp_path, "sk-alice") is None


def test_unparseable_expiry_fails_closed(tmp_path, monkeypatch):
    db = _mk_db(tmp_path / "webui.db", [("dave@example.com", "sk-dave", "soon")])
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    assert owui_api_keys.resolve_email(tmp_path, "sk-dave") is None
