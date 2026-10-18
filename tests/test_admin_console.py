"""Console administration (hubzoid.admin): admin.yaml plans, the settings
registry, bounded read-only diagnostics, support bundles, monitoring --json
and the read-only admin tools on hosted MCP."""
from __future__ import annotations

import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hubzoid.admin import config as admin_cfg
from hubzoid.admin import diagnostics as diag
from hubzoid.admin.config import AdminError
from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-SECRET-123")  # must never leak
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    return TestClient(build_app())


def test_admin_schema_defaults_and_bounds():
    d = admin_cfg.defaults()["diagnostics"]
    assert d == {"check_timeout_seconds": 10, "max_parallel_checks": 4, "bundle_ttl_minutes": 60,
                 "allow_network_probes": False}  # no report retention by default (B037)
    with pytest.raises(AdminError) as exc:
        admin_cfg.validate({"version": 1, "diagnostics": {"check_timeout_seconds": 99}})
    assert exc.value.extra["pointer"] == "/diagnostics/check_timeout_seconds"
    assert exc.value.extra["accepted"] == "1 to 30"


def test_unknown_key_reports_yaml_path(hub):
    (hub / "admin.yaml").write_text("version: 1\ndiagnostics:\n  timeout: 3\n")
    with pytest.raises(AdminError) as exc:
        admin_cfg.service_for(hub).parse_file(hub / "admin.yaml")
    assert exc.value.extra["line"] == 3


def test_plan_digest_stale_revision_and_apply(client):
    values = {"version": 1, "diagnostics": {"allow_network_probes": True}}
    plan = client.post("/api/admin/config/plans", headers=ORIGIN,
                       json={"values": values, "expected_revision": 0}).json()
    assert plan["diff"] == [{"pointer": "/diagnostics/allow_network_probes", "old": False, "new": True}]
    wrong = client.post(f"/api/admin/config/plans/{plan['plan_id']}/apply", headers=ORIGIN,
                        json={"digest": "sha256:00", "expected_revision": 0})
    assert wrong.status_code == 409
    ok = client.post(f"/api/admin/config/plans/{plan['plan_id']}/apply", headers=ORIGIN,
                     json={"digest": plan["digest"], "expected_revision": 0})
    assert ok.status_code == 200 and ok.json()["revision"] == 1
    again = client.post("/api/admin/config/plans", headers=ORIGIN,
                        json={"values": values, "expected_revision": 0})
    assert again.status_code == 409 and again.json()["detail"]["current_revision"] == 1
    shown = client.get("/api/admin/config").json()
    assert shown["provenance"]["/diagnostics/allow_network_probes"] == {"value": True, "source": "deployment"}


def test_idempotency_payload_mismatch(client):
    h = dict(ORIGIN, **{"Idempotency-Key": "plan-1"})
    a = client.post("/api/admin/config/plans", headers=h, json={"values": {"version": 1}, "expected_revision": 0})
    b = client.post("/api/admin/config/plans", headers=h, json={"values": {"version": 1}, "expected_revision": 0})
    assert a.json()["plan_id"] == b.json()["plan_id"]
    c = client.post("/api/admin/config/plans", headers=h,
                    json={"values": {"version": 1, "diagnostics": {"max_parallel_checks": 2}}, "expected_revision": 0})
    assert c.status_code == 409 and c.json()["detail"]["code"] == "idempotency_key_reused"


def test_domain_registry_has_no_duplicate_keys(client):
    items = client.get("/api/admin/domains").json()["items"]
    names = [d["domain"] for d in items]
    assert len(names) == len(set(names)) and {"chat", "branding", "admin", "model"} <= set(names)
    assert not any("SECRET" in json.dumps(d) for d in items)


def test_passive_diagnostics_complete_without_secrets(hub):
    svc = diag.service_for(hub)
    started = svc.start(actor="t", run_inline=True)
    report = svc.get(started["report_id"])
    assert report["status"] == "completed"
    ids = {c["check_id"] for c in report["checks"]}
    assert "connectors.reachability" not in ids and "model.signin" in ids
    assert all(c["status"] in diag.STATUSES for c in report["checks"])
    assert "SECRET" not in json.dumps(report)


def test_network_probe_requires_double_opt_in(hub):
    svc = diag.service_for(hub)
    with pytest.raises(AdminError) as exc:
        svc.start(actor="t", profile="active", consent_network=True)
    assert exc.value.status == 403  # admin.yaml has not allowed probes
    cfg = admin_cfg.service_for(hub)
    plan = cfg.plan({"version": 1, "diagnostics": {"allow_network_probes": True}}, expected_revision=0, actor="t")
    cfg.apply(plan["plan_id"], digest=plan["digest"], expected_revision=0, actor="t")
    with pytest.raises(AdminError) as exc:
        svc.start(actor="t", profile="active")
    assert exc.value.status == 422  # no consent for this run
    ok = svc.start(actor="t", profile="active", consent_network=True, run_inline=True)
    assert "connectors.reachability" in {c["check_id"] for c in svc.get(ok["report_id"])["checks"]}


def test_report_lease_interrupted_not_rerun(hub, monkeypatch):
    svc = diag.service_for(hub)
    with svc.engine.begin() as conn:
        conn.execute(diag.reports.insert().values(
            id="r-dead", deployment_id=svc.deployment_id, hub_id=None, requested_by_user_id="t",
            profile="passive", check_ids_json="[]", status="running", created_at=time.time() - 100,
            lease_owner="gone", lease_expires_at=time.time() - 10, config_revision=0, cancel_requested=0))
    assert svc.recover() == 1
    assert svc.get("r-dead")["status"] == "interrupted"
    assert svc.get("r-dead")["checks"] == []
    # A late worker of the dead process writes nothing over the interruption.
    monkeypatch.setattr(diag, "WORKER_ID", "gone")
    svc._record("r-dead", diag.CHECKS[0], diag.Result("ok", "late"), 1)
    assert svc.get("r-dead")["checks"] == [] and svc.get("r-dead")["status"] == "interrupted"


def test_concurrent_retry_admits_one_report(hub):
    import sqlalchemy as sa

    svc = diag.service_for(hub)
    first = svc.start(actor="t", idempotency_key="k-1", run_inline=True)
    again = svc.start(actor="t", idempotency_key="k-1", run_inline=True)
    assert again["report_id"] == first["report_id"] and again["replayed"]
    # A retry that missed the lookup still can't admit a second report.
    with pytest.raises(sa.exc.IntegrityError):
        svc._admit(actor="t", profile="passive", selected=[], hub_id=None, idempotency_key="k-1", rhash="x",
                   now=time.time(), retention=None, previous_report_id=None)
    with pytest.raises(AdminError) as exc:  # the request hash covers every input
        svc.start(actor="t", idempotency_key="k-1", previous_report_id=first["report_id"])
    assert exc.value.code == "idempotency_key_reused"


def test_slow_check_times_out(hub, monkeypatch):
    def slow(_hub):
        time.sleep(3)
        return diag.Result("ok", "late")

    monkeypatch.setattr(diag, "BY_ID", {**diag.BY_ID, "storage.disk": diag.CheckDef(
        "storage.disk", "passive", "x", "HUB-109", slow)})
    cfg = admin_cfg.service_for(hub)
    plan = cfg.plan({"version": 1, "diagnostics": {"check_timeout_seconds": 1}}, expected_revision=0, actor="t")
    cfg.apply(plan["plan_id"], digest=plan["digest"], expected_revision=0, actor="t")
    svc = diag.service_for(hub)
    rid = svc.start(actor="t", check_ids=["storage.disk"], run_inline=True)["report_id"]
    check = svc.get(rid)["checks"][0]
    assert check["status"] == "timeout" and check["remediation_code"] == "check_timeout"


def test_bundle_allowlist_limits_and_expiry(client, hub):
    rid = diag.service_for(hub).start(actor="t", run_inline=True)["report_id"]
    b = client.post(f"/api/admin/diagnostics/reports/{rid}/bundles", headers=ORIGIN)
    assert b.status_code == 201, b.text
    data = client.get(b.json()["download"]).content
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(zf.namelist()) == ["checks.json", "config-provenance.json", "manifest.json", "versions.json"]
        blob = b"".join(zf.read(n) for n in zf.namelist())
    assert b"SECRET" not in blob and str(hub).encode() not in blob
    with zipfile.ZipFile(io.BytesIO(data)) as zf:  # the manifest hashes the archived bytes
        files = json.loads(zf.read("manifest.json"))["files"]
        import hashlib

        assert all(hashlib.sha256(zf.read(n)).hexdigest() == h for n, h in files.items())
    with diag.service_for(hub).engine.begin() as conn:
        conn.execute(diag.bundles.update().values(expires_at=time.time() - 1))
    expired = client.get(b.json()["download"])
    assert expired.status_code == 410 and expired.json()["detail"]["code"] == "download_expired"
    diag.service_for(hub).recover()
    kept = diag.service_for(hub).bundle_dir / f"{b.json()['bundle_id']}.zip"
    assert kept.is_file()  # the link expired, the bundle is kept
    renewed = client.post(f"/api/admin/diagnostics/reports/{rid}/bundles", headers=ORIGIN).json()
    assert renewed["bundle_id"] == b.json()["bundle_id"] and client.get(renewed["download"]).status_code == 200
    gone = client.delete(f"/api/admin/diagnostics/bundles/{renewed['bundle_id']}", headers=ORIGIN)
    assert gone.status_code == 200 and not kept.exists()


def test_hub_admin_cannot_read_other_hub_report(client, hub, monkeypatch):
    from hubzoid.access import store_for

    from hubzoid import deployment

    other_dir = hub.parent / "other-hub"
    other_dir.mkdir()
    real_hubs = deployment.hubs
    monkeypatch.setattr(deployment, "hubs", lambda d: real_hubs(d) + [
        {"key": "other-hub", "name": "Other", "path": str(other_dir), "model_id": "other"}])
    store_for(hub).grant("ana@example.org", hub.name, "manage_access", actor="test")
    svc = diag.service_for(hub)
    with pytest.raises(AdminError) as exc:
        svc.start(actor="t", hub_id="not-a-hub")
    assert exc.value.status == 404
    seen = []
    monkeypatch.setattr(diag, "BY_ID", {**diag.BY_ID, "storage.disk": diag.CheckDef(
        "storage.disk", "passive", "x", "HUB-109", lambda h: seen.append(h) or diag.Result("ok", "x"))})
    other = svc.start(actor="t", hub_id="other-hub", check_ids=["storage.disk"], run_inline=True)["report_id"]
    assert seen == [other_dir]  # the named hub is examined, not the receiving bridge's
    mine = svc.start(actor="t", hub_id=hub.name, run_inline=True)["report_id"]
    as_people(monkeypatch)
    assert client.get(f"/api/admin/diagnostics/reports/{other}", headers=who("ana")).status_code == 404
    assert client.get(f"/api/admin/diagnostics/reports/{mine}", headers=who("ana")).status_code == 200
    listed = client.get("/api/admin/diagnostics/reports", headers=who("ana")).json()["items"]
    assert {r["report_id"] for r in listed} == {mine}
    assert client.get("/api/admin/config", headers=who("ana")).status_code == 403  # deployment-wide
    assert client.get("/api/admin/capabilities", headers=who("ben")).status_code == 403  # no admin.access


def test_monitoring_commands_json_match(hub):
    from hubzoid.cli import app as cli_app

    run = CliRunner().invoke
    for args in (["schedule", "list", str(hub), "--json"], ["access", "list", str(hub), "--json"],
                 ["access", "check", "ana@example.org", str(hub), "--json"],
                 ["admin", "list", str(hub), "--json"], ["admin", "config", "show", str(hub), "--json"],
                 ["admin", "capabilities", str(hub), "--json"]):
        out = run(cli_app, args)
        assert out.exit_code == 0, (args, out.output)
        json.loads(out.output)
    report = run(cli_app, ["admin", "diagnostics", "run", str(hub), "--wait", "--json"])
    assert json.loads(report.output)["status"] == "completed"


def test_mcp_admin_tools_require_grant_and_hide_secrets(hub):
    import asyncio

    from hubzoid.access import store_for
    from hubzoid.access.identity import Identity, identity_scope
    from hubzoid.admin import mcp_tools

    tools = {t.name: t for t in mcp_tools.make(hub)}

    def call(name, email):
        async def go():
            with identity_scope(Identity.make(user=email, surface="mcp")):
                return await tools[name].on_invoke_tool(None, "{}")
        return asyncio.run(go())

    assert call("agent_health", "ben@example.org").startswith("[access denied:")
    store_for(hub).grant("ben@example.org", hub.name, "manage_access", actor="test")
    out = call("agent_health", "ben@example.org")
    assert json.loads(out)["agent"] == hub.name.lower() and "SECRET" not in out
    assert "denials" in json.loads(call("recent_denials", "ben@example.org"))
