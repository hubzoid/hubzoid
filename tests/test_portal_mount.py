"""The portal mounts on a FastAPI app: API + static SPA (when built)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import hubzoid.access as access
import hubzoid.db as db
from hubzoid import portal


def test_mount_serves_api_and_static(tmp_path, monkeypatch):
    eng = db.engine_for(tmp_path)  # real per-hub sqlite under tmp
    access._stores.clear()

    app = FastAPI()
    portal.mount_portal(app, tmp_path)
    c = TestClient(app)

    # API is mounted; unauthenticated -> 403 (no dev user, no OWUI session)
    monkeypatch.delenv("HUBZOID_PORTAL_DEV_USER", raising=False)
    assert c.get("/portal/api/me").status_code == 403

    # dev user that is an org admin -> 200 (dev override is a two-part opt-in)
    access.store_for(tmp_path).bootstrap(["dev@corp"], authoritative=True)
    monkeypatch.setenv("HUBZOID_PORTAL_DEV_USER", "dev@corp")
    # DEV_USER alone is not trusted without the explicit DEV flag
    monkeypatch.delenv("HUBZOID_PORTAL_DEV", raising=False)
    assert c.get("/portal/api/me").status_code == 403
    monkeypatch.setenv("HUBZOID_PORTAL_DEV", "1")
    r = c.get("/portal/api/me")
    assert r.status_code == 200 and r.json()["org_admin"] is True

    # static SPA is served if built
    dist = Path(__file__).resolve().parents[1] / "hubzoid" / "portal_dist" / "index.html"
    if dist.exists():
        r = c.get("/portal/")
        assert r.status_code == 200 and "<div id=\"root\">" in r.text
    assert eng is not None
