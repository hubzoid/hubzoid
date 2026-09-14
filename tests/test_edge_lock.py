"""The OWUI access-UI lock in the edge proxy."""
from __future__ import annotations

from starlette.testclient import TestClient

from hubzoid.edge import _owui_lock_prefixes, build_edge_app


def test_lock_prefixes_env():
    assert _owui_lock_prefixes({}) == ()
    assert _owui_lock_prefixes({"HUBZOID_LOCK_OWUI_ACCESS_UI": "1"}) == ("/api/v1/groups",)
    assert _owui_lock_prefixes({
        "HUBZOID_LOCK_OWUI_ACCESS_UI": "true",
        "HUBZOID_OWUI_LOCKED_PREFIXES": "/a,/b",
    }) == ("/a", "/b")


def test_locked_route_returns_403(monkeypatch):
    monkeypatch.setenv("HUBZOID_LOCK_OWUI_ACCESS_UI", "1")
    app = build_edge_app(default_base="http://127.0.0.1:1", routes=())
    with TestClient(app) as c:
        # a browser write to OWUI group management is blocked, before any upstream
        r = c.post("/api/v1/groups", json={"name": "x"})
        assert r.status_code == 403
        assert "portal" in r.text.lower()


def test_unlocked_allows_through(monkeypatch):
    monkeypatch.delenv("HUBZOID_LOCK_OWUI_ACCESS_UI", raising=False)
    app = build_edge_app(default_base="http://127.0.0.1:1", routes=())
    with TestClient(app) as c:
        # not locked -> not a 403 from us (upstream is down -> 502)
        r = c.post("/api/v1/groups", json={"name": "x"})
        assert r.status_code != 403
