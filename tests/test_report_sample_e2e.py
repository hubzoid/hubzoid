"""Local end-to-end sample: synthetic per-person data -> report from the shared
template -> publish -> preview email -> open in the viewer. Runs the example in
docs/examples/report on a real DBOS engine (subprocess). Model-free, no SMTP.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "examples" / "report"
ALICE, BOB = "alice@company.com", "bob@company.com"

_CSV = """owner,week,region,orders,revenue
alice@company.com,2026-W37,Chennai,12,48000
alice@company.com,2026-W38,Coimbatore,15,52500
bob@company.com,2026-W38,Bengaluru-SECRET,40,99000
"""

_RUN = """
import json, sys
from hubzoid.access import store_for
from hubzoid.workflows import runtime
for who in (%r, %r):
    store_for(sys.argv[1]).upsert_identity(email=who, owui_id="id-" + who)
runtime.init(sys.argv[1], hub_name="sales")
runtime.load_workflows(sys.argv[1])
runtime.launch()
print("RESULT " + json.dumps(runtime.run_now("weekly_report")), flush=True)
runtime.shutdown()
""" % (ALICE, BOB)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    d = tmp_path / "sales"
    wf = d / "workflows" / "weekly_report"
    wf.mkdir(parents=True)
    (d / "AGENTS.md").write_text("---\nname: sales\n---\nbody")
    shutil.copy(EXAMPLE / "weekly_report.py", wf / "main.py")
    shutil.copy(EXAMPLE / "report_template.html", wf / "report_template.html")
    (d / "raw_data").mkdir()
    (d / "raw_data" / "orders.csv").write_text(_CSV)
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_SMTP_HOST")}
    env.update(HUBZOID_OPERATIONAL_DB=f"sqlite:///{tmp_path / 'ops.db'}",
               HUBZOID_DBOS_DB=f"sqlite:///{tmp_path / 'dbos.db'}", WEBUI_AUTH="true",
               HUBZOID_WORKFLOW_USER=ALICE, HUBZOID_EMAIL_DELIVERY="preview",
               HUBZOID_PUBLIC_URL="https://hub.example.com")
    for k, v in env.items():
        if k.startswith(("HUBZOID_", "WEBUI_")):
            monkeypatch.setenv(k, v)
    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    return d, env


def test_sample_report_end_to_end(hub, monkeypatch):
    from hubzoid import artifacts as arts
    from hubzoid.access import session
    from hubzoid.artifacts import web as artifacts_web

    hub_dir, env = hub
    proc = subprocess.run([sys.executable, "-c", _RUN, str(hub_dir)], env=env,
                          capture_output=True, text=True, timeout=180)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, proc.stderr[-3000:]
    result = json.loads(line[7:])
    assert result["rows"] == 2 and result["email"] == "previewed"

    art = arts.get(hub_dir, result["report"])
    assert art.owner == ALICE and art.kind == "html"
    page = arts.content_path(hub_dir, art).read_text()
    assert "Coimbatore" in page and "Chennai" in page
    assert "SECRET" not in page and BOB not in page                  # only alice's rows

    eml = next((hub_dir / ".hubzoid" / "outbox").rglob("*.eml")).read_text()
    assert f"To: {ALICE}" in eml
    assert f"https://hub.example.com/portal/artifacts/{art.id}" in eml

    def fake_verified(request, hub_dir=None):
        token = request.cookies.get("token") or ""
        return token.split(":", 1)[1] if token.startswith("session:") else ""

    monkeypatch.setattr(session, "verified_email", fake_verified)
    app = FastAPI()
    app.include_router(artifacts_web.build_router(hub_dir))
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("token", f"session:{ALICE}")
    content = client.get(f"/portal/artifacts/{art.id}/content")
    assert content.status_code == 200 and "Weekly sales report" in content.text
    client.cookies.set("token", f"session:{BOB}")
    assert client.get(f"/portal/artifacts/{art.id}/content").status_code == 404
