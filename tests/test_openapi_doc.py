"""The API reference lists the public API only (HUB-140 F9, founder decision D2)."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

MINIMAL = Path(__file__).parent / "fixtures" / "minimal_hub"


def test_the_reference_is_the_public_api_with_the_real_version(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    shutil.copytree(MINIMAL, hub, ignore=shutil.ignore_patterns(".hubzoid"))
    monkeypatch.delenv("HUBZOID_AUTH", raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "hubzoid-test/scripted")
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "1")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    from hubzoid import __version__
    from hubzoid.server import build_app

    client = TestClient(build_app(), base_url="http://127.0.0.1")
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Hubzoid API" and schema["info"]["version"] == __version__
    paths = set(schema["paths"])
    assert {"/v1/chat/completions", "/v1/models", "/api/agui", "/api/requests"} <= paths
    assert not [p for p in paths if p.startswith(("/internal", "/api/parity", "/portal", "/api/admin"))]
    assert schema["security"] == [{"personalKey": []}]
    assert client.get("/api/openapi.json").json()["paths"].keys() == schema["paths"].keys()
