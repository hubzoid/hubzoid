"""Readiness to move off Open WebUI (HUB-85, prepared part): read-only inventory."""
from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine

from hubzoid import migration_readiness as mr


def _owui(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, name TEXT, role TEXT, oauth TEXT);
        CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, chat TEXT, archived INTEGER, pinned INTEGER);
        CREATE TABLE folder (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE function (id TEXT PRIMARY KEY, type TEXT, content TEXT);
        CREATE TABLE file (id TEXT PRIMARY KEY, meta TEXT);
        CREATE TABLE alembic_version (version_num TEXT);
        INSERT INTO alembic_version VALUES ('abc123');
        INSERT INTO "user" VALUES ('u1', 'a@example.com', 'A', 'admin', '{"oidc": {"sub": "1"}}'),
                                  ('u2', 'b@example.com', 'B', 'pending', 'null');
        INSERT INTO chat VALUES ('c1', 'u1', '{}', 0, 1), ('c2', 'u1', '{}', 1, 0);
        INSERT INTO folder VALUES ('f1', 'Ops');
        INSERT INTO function VALUES ('fn1', 'filter', 'import os'), ('fn2', 'pipe', 'x');
        INSERT INTO file VALUES ('x1', '{"size": 2048}');
    """)
    db.commit()
    db.close()


def test_inventory_counts_maps_and_blocks_without_reading_content(tmp_path):
    path = tmp_path / "webui.db"
    _owui(path)
    before = path.read_bytes()
    r = mr.inventory(create_engine(f"sqlite:///{path}"))
    d = r.to_dict()
    cats = {c["key"]: c for c in d["categories"]}
    assert cats["accounts"]["count"] == 2 and cats["accounts"]["by_role"] == {"admin": 1, "pending": 1}
    assert cats["oauth_accounts"]["count"] == 1  # "null" is not a sign-in
    assert cats["pinned_chats"]["status"] == "prepared" and cats["folders"]["status"] == "prepared"
    assert cats["functions"]["status"] == "blocked" and cats["functions"]["by_type"] == {"filter": 1, "pipe": 1}
    assert cats["files"]["bytes"] == 2048 and cats["notes"]["status"] == "absent"
    assert d["verdict"] == "blocked" and d["writes"] == "none" and d["digest"].startswith("sha256:")
    assert "a@example.com" not in str(d) and "import os" not in str(d)  # counts only, never content
    assert path.read_bytes() == before
    assert [m["id"] for m in d["matrix"]] == [f"M{n:02d}" for n in range(1, 14)]
    assert "| Folders | 1 |" in mr.to_markdown(r)
