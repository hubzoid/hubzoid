"""The Console administration API (hubzoid.admin.routes): capabilities,
overview, admin.yaml validation, export and import, settings domains, and
diagnostic reports, with each refusal. The local owner is the administrator."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, build_app, make_hub


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-SECRET-456")  # must never leak
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def test_capabilities_and_overview(client):
    caps = client.get("/api/admin/capabilities").json()
    assert caps["principal"]["org_admin"] is True and caps["capabilities"] and caps["diagnostics"]
    assert client.get("/api/admin/overview?days=abc").status_code == 422
    over = client.get("/api/admin/overview?days=500")
    assert over.status_code == 200 and "SECRET-456" not in over.text


def test_admin_yaml_validate_export_and_import(client):
    ok = client.post("/api/admin/config/validate", headers=ORIGIN,
                     json={"values": {"version": 1, "diagnostics": {"check_timeout_seconds": 5}}}).json()
    assert ok["valid"] is True and ok["values"]["diagnostics"]["check_timeout_seconds"] == 5
    bad = client.post("/api/admin/config/validate", headers=ORIGIN,
                      json={"values": {"version": 1, "diagnostics": {"check_timeout_seconds": 99}}}).json()
    assert bad["valid"] is False and bad["error"]["pointer"] == "/diagnostics/check_timeout_seconds"

    assert client.get("/api/admin/config").status_code == 200
    assert client.get("/api/admin/config/plans").json()["items"] == []
    other = client.post("/api/admin/config/plans", headers=ORIGIN, json={"domain": "chat", "values": {}})
    assert other.status_code == 422 and "with their owner" in other.json()["detail"]["message"]

    yaml_out = client.get("/api/admin/config/export")
    assert yaml_out.status_code == 200 and yaml_out.headers["content-type"].startswith("application/yaml")
    assert 'filename="admin.yaml"' in yaml_out.headers["content-disposition"]
    bundle = client.get("/api/admin/config/export?format=bundle").json()
    assert bundle["format"] == "bundle" and bundle["domain"] == "admin" and "values" in bundle
    assert client.get("/api/admin/config/export?format=xml").status_code == 422

    url = "/api/admin/config/import/validate"
    assert client.post(url, headers=ORIGIN, content=b"x" * (1024 * 1024 + 1)).status_code == 413
    assert client.post(url, headers=ORIGIN, content=b"{nope").status_code == 400
    assert client.post(url, headers=ORIGIN, json={"domain": "chat", "format": "bundle"}).status_code == 422
    checked = client.post(url, headers=ORIGIN, json={**bundle, "surprise": 1}).json()
    assert checked["valid"] is True and checked["unsupported_fields"] == ["surprise"]
    assert checked["next"].startswith("POST /api/admin/config/plans")
    broken = client.post(url, headers=ORIGIN, json={**bundle, "values": {"version": 2}}).json()
    assert broken["valid"] is False and broken["error"]["code"]


def test_settings_domains(client):
    items = client.get("/api/admin/domains").json()["items"]
    assert items and all("domain" in d for d in items)
    readable = next(d for d in items if d.get("can_read"))
    assert client.get(f"/api/admin/domains/{readable['domain']}").json()["domain"] == readable["domain"]
    missing = client.get("/api/admin/domains/no-such-domain")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "not_found"


def test_a_diagnostic_report_runs_and_can_be_bundled(client):
    catalog = client.get("/api/admin/diagnostics/checks").json()["items"]
    assert catalog
    started = client.post("/api/admin/diagnostics/reports", headers=ORIGIN, json={})
    assert started.status_code == 202, started.text
    rid = started.json()["report_id"]
    deadline = time.time() + 20
    report = {}
    while time.time() < deadline:
        report = client.get(f"/api/admin/diagnostics/reports/{rid}").json()
        if report.get("status") not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert report["status"] == "completed", report
    assert report["checks"] and all(c["status"] for c in report["checks"])
    assert "SECRET-456" not in json.dumps(report)
    listed = client.get("/api/admin/diagnostics/reports").json()
    assert rid in json.dumps(listed)
    assert client.get("/api/admin/diagnostics/reports/no-such-report").status_code == 404

    made = client.post(f"/api/admin/diagnostics/reports/{rid}/bundles", headers=ORIGIN)
    assert made.status_code == 201, made.text
    bid = made.json()["bundle_id"]
    assert client.get(f"/api/admin/diagnostics/bundles/{bid}").status_code == 200
    download = client.get(f"/api/admin/diagnostics/bundles/{bid}/download")
    assert download.status_code == 200 and b"SECRET-456" not in download.content
    assert client.delete(f"/api/admin/diagnostics/bundles/{bid}", headers=ORIGIN).status_code == 200
    assert client.get(f"/api/admin/diagnostics/bundles/{bid}").status_code == 410  # deleted: gone
    assert client.get("/api/admin/diagnostics/bundles/no-such-bundle").status_code == 404
