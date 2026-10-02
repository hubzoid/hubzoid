"""Regressions for fixes made while wrapping up the web app release.

- The edge redacts one-time sign-in secrets from its access log too.
- Open WebUI API keys open the Console API only in Open WebUI mode.
- `hubzoid backup` finds a web app gateway's data folder from its manifest.
- The upgrade guard does not count the local owner as a migrated account.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from starlette.requests import Request


def test_edge_installs_the_sign_in_log_redaction():
    from hubzoid import edge
    from hubzoid.auth.logredact import RedactCredentials

    access = logging.getLogger("uvicorn.access")
    for f in [f for f in access.filters if isinstance(f, RedactCredentials)]:
        access.removeFilter(f)
    edge._redact_oauth_callback_logs()
    assert any(isinstance(f, RedactCredentials) for f in access.filters)


def _request(auth: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/portal/api/me",
                    "headers": [(b"authorization", auth.encode())]})


def test_open_webui_api_keys_only_in_the_legacy_mode(monkeypatch):
    from hubzoid import portal

    monkeypatch.delenv("HUBZOID_UI", raising=False)
    assert portal.api_key(_request("Bearer sk-old-key")) is None
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    assert portal.api_key(_request("Bearer sk-old-key")) == "sk-old-key"


def test_backup_finds_a_web_app_gateways_data_folder(tmp_path, monkeypatch):
    from hubzoid import backup, deployment

    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    hubs = []
    for key in ("ops", "finance"):
        hub = tmp_path / key
        hub.mkdir()
        (hub / "AGENTS.md").write_text("# hub\n")
        hubs.append({"key": key, "name": key, "path": str(hub), "model_id": key, "slug": key})
    data = tmp_path / "gateway-data"
    data.mkdir()
    deployment.save(data / "deployment.json", hubs=hubs,
                    operational_url=f"sqlite:///{data / 'operational.db'}",
                    owui_url="", owui_db="", ui_mode="hubzoid", auth=True)
    plan = backup.plan(tmp_path / "ops")
    assert data.resolve() in {r.path.resolve() for r in plan.roots}
    assert tmp_path.resolve() not in {r.path.resolve() for r in plan.roots}


def test_upgrade_guard_ignores_the_local_owner(tmp_path, monkeypatch):
    from hubzoid import upgrade

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("HUBZOID_OPERATIONAL_DB", raising=False)
    hub = tmp_path / "hub"
    (hub / ".hubzoid").mkdir(parents=True)
    con = sqlite3.connect(hub / ".hubzoid" / "hub.db")
    con.execute("CREATE TABLE hz_users (id TEXT PRIMARY KEY, email TEXT)")
    con.execute("INSERT INTO hz_users VALUES ('o1', 'admin@localhost')")
    con.commit()
    assert upgrade.hubzoid_accounts(hub) == 0
    con.execute("INSERT INTO hz_users VALUES ('p1', 'person@example.com')")
    con.commit()
    con.close()
    assert upgrade.hubzoid_accounts(hub) == 1
