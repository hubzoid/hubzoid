"""Artifact download links are signed with the hub's own secret, not the bridge
API key (whose default, "dev", is public). The public /artifacts route rejects
forged links, links for another hub, expired links and the default key.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from hubzoid import _signing
from hubzoid import memory as memlib


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: links\ndescription: d\n---\nbody")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("HUBZOID_ARTIFACT_SECRET", raising=False)
    monkeypatch.delenv("HUBZOID_ARTIFACT_LINK_TTL", raising=False)
    adir = memlib.chat_artifact_dir(hub, "c1")
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "report.txt").write_text("quarterly numbers")
    return hub


def _client(monkeypatch, keys="dev"):
    monkeypatch.setenv("BRIDGE_API_KEYS", keys)
    from hubzoid.server import build_app
    return TestClient(build_app())


def _query(query: str) -> str:
    return f"/artifacts/c1/report.txt?{query}"


def test_secret_is_generated_per_hub_and_private(hub):
    token = _signing.sign_artifact_path("c1", "report.txt", hub_dir=hub)
    secret = hub / ".hubzoid" / "artifact_secret"
    assert secret.is_file()
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert len(secret.read_text().strip()) == 64
    # stable across calls, different for another hub
    assert _signing.sign_artifact_path("c1", "report.txt", hub_dir=hub) == token
    other = hub.parent / "other"
    other.mkdir()
    assert _signing.sign_artifact_path("c1", "report.txt", hub_dir=other) != token


def test_issued_link_downloads(hub, monkeypatch):
    client = _client(monkeypatch)
    r = client.get(_query(_signing.artifact_query("c1", "report.txt", hub_dir=hub)))
    assert r.status_code == 200
    assert r.text == "quarterly numbers"


def test_link_forged_with_default_bridge_key_is_rejected(hub, monkeypatch):
    client = _client(monkeypatch)
    forged = hmac.new(b"dev", b"c1/report.txt", hashlib.sha256).hexdigest()[:16]
    assert client.get(_query(f"t={forged}")).status_code == 401


def test_link_from_another_hub_is_rejected(hub, monkeypatch):
    client = _client(monkeypatch)
    other = hub.parent / "other"
    other.mkdir()
    foreign = _signing.sign_artifact_path("c1", "report.txt", hub_dir=other)
    assert client.get(_query(f"t={foreign}")).status_code == 401


def test_default_key_is_not_accepted_on_public_route(hub, monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/artifacts/c1/report.txt", headers={"Authorization": "Bearer dev"})
    assert r.status_code == 401


def test_real_bridge_key_still_works_for_api_callers(hub, monkeypatch):
    client = _client(monkeypatch, keys="k-7f3a9c")
    r = client.get("/artifacts/c1/report.txt", headers={"Authorization": "Bearer k-7f3a9c"})
    assert r.status_code == 200


def test_links_never_expire_by_default(hub):
    assert "&e=" not in _signing.artifact_query("c1", "report.txt", hub_dir=hub)


def test_ttl_links_expire(hub, monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setenv("HUBZOID_ARTIFACT_LINK_TTL", "60")
    query = _signing.artifact_query("c1", "report.txt", hub_dir=hub)
    assert "&e=" in query
    assert client.get(_query(query)).status_code == 200

    past = int(time.time()) - 5
    token = _signing._mac("c1", "report.txt", past, hub)
    assert client.get(_query(f"t={token}&e={past}")).status_code == 401

    # extending the expiry breaks the signature
    token, exp = query.split("t=")[1].split("&e=")
    assert client.get(_query(f"t={token}&e={int(exp) + 3600}")).status_code == 401


def test_env_secret_overrides_file(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_ARTIFACT_SECRET", "shared-across-hosts")
    expected = hmac.new(
        b"shared-across-hosts", b"c1/report.txt", hashlib.sha256
    ).hexdigest()[:32]
    assert _signing.sign_artifact_path("c1", "report.txt", hub_dir=hub) == expected
    assert not (hub / ".hubzoid" / "artifact_secret").exists()


def test_deleting_the_secret_revokes_old_links(hub, monkeypatch):
    client = _client(monkeypatch)
    old = _signing.artifact_query("c1", "report.txt", hub_dir=hub)
    os.remove(hub / ".hubzoid" / "artifact_secret")
    assert client.get(_query(old)).status_code == 401
