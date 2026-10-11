"""The Console for workflows from chat: report grants in Access, people's
schedules (admins pause, resume their own pause, stop) and live report links."""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import hubzoid.access as access
import hubzoid.db as db
from hubzoid import artifacts as arts
from hubzoid.portal import PortalAdmin, build_router

WORKFLOW = '''
from hubzoid import workflow
@workflow(as_tool=True, schedulable=True, sensitive=True)
def program_report():
    return {}
'''


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for k in ("HUBZOID_SHARE_DOMAINS", "HUBZOID_PUBLIC_URL", "WEBUI_URL"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "workflows" / "program_report").mkdir(parents=True)
    (tmp_path / "workflows" / "program_report" / "main.py").write_text(WORKFLOW)
    eng = create_engine(f"sqlite:///{tmp_path / 'hub.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    gs = access.store_for(tmp_path)
    gs.grant("root", "*", "manage_access", actor="test")
    app = FastAPI()
    app.include_router(build_router(tmp_path, admin_resolver=lambda _r: PortalAdmin(
        subject="root", is_org_admin=True, manageable=[])))
    c = TestClient(app)
    c.headers.update({"origin": "http://testserver"})
    c.hub, c.path, c.eng = tmp_path.name, tmp_path, eng   # type: ignore[attr-defined]
    return c


def _schedule(c, state="active", reason=None, account=None):
    from hubzoid.workflows import personal

    now = time.time()
    personal._engine(c.path)   # tables at head
    with c.eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO hz_personal_schedules (id, hub, workflow, owner, owner_account, name, inputs, "
            "cadence, timezone, state, reason, next_fire, failures, created, updated) VALUES "
            "('ps1', :h, 'program_report', 'meera@co.org', :a, 'Bhava monthly', :i, "
            "'every monday at 09:00', 'Asia/Kolkata', :s, :r, :n, 0, :t, :t)"),
            {"h": c.hub, "i": json.dumps({"program": "Bhava"}), "s": state, "r": reason,
             "n": now + 3600, "t": now, "a": account})
        # What the engine publishes at start, so a Console in any process can show inputs.
        conn.execute(text("INSERT INTO hz_meta (k, v) VALUES (:k, :v)"),
                     {"k": f"wf_inputs:{c.hub}", "v": json.dumps({"program_report": {"properties": {"program": {}}}})})


def test_access_lists_one_grant_per_report_plus_scheduling_and_company_sharing(client):
    perms = {p["permission"]: p for p in client.get(
        "/portal/api/permissions", params={"hub": client.hub}).json()["permissions"]}
    assert perms["wf_program_report"]["label"] == "Run Program report"
    assert perms["wf_program_report"]["sensitive"] is True
    assert {"schedule_reports", "share_domain_links"} <= set(perms)


def test_admins_see_schedules_and_pause_or_stop_but_resume_only_their_own_pause(client):
    _schedule(client)
    rows = client.get("/portal/api/schedules", params={"hub": client.hub}).json()["schedules"]
    assert rows[0]["owner"] == "meera@co.org" and rows[0]["when"] == "Mondays at 09:00"
    assert rows[0]["inputs"] == "Bhava" and rows[0]["cost_30d"] == 0
    assert client.post("/portal/api/schedules/ps1/pause", json={"hub": client.hub}).status_code == 200
    row = client.get("/portal/api/schedules", params={"hub": client.hub}).json()["schedules"][0]
    assert row["state_label"] == "Paused by admin"
    with client.eng.begin() as conn:
        conn.execute(text("UPDATE hz_personal_schedules SET reason='failures'"))
    refused = client.post("/portal/api/schedules/ps1/resume", json={"hub": client.hub})
    assert refused.status_code == 409 and "only schedules they paused" in refused.json()["detail"]
    assert client.post("/portal/api/schedules/ps1/stop", json={"hub": client.hub}).status_code == 200
    assert client.get("/portal/api/schedules", params={"hub": client.hub}).json()["schedules"] == []


def test_admins_turn_off_any_company_link_and_copy_only_team_ones(client, tmp_path, monkeypatch):
    for k, v in {"HUBZOID_SHARE_DOMAINS": "co.org", "GOOGLE_CLIENT_ID": "i", "GOOGLE_CLIENT_SECRET": "s",
                 "ENABLE_OAUTH_SIGNUP": "true", "OAUTH_ALLOWED_DOMAINS": "co.org"}.items():
        monkeypatch.setenv(k, v)
    gs = access.store_for(client.path)
    for who in ("reports@co.org", "meera@co.org"):
        gs.upsert_identity(email=who, owui_id="id-" + who)
        gs.grant(who, client.hub, "use_hub", actor="t")
        gs.grant(who, client.hub, "share_domain_links", actor="t")
    made = {}
    for who, scope in (("reports@co.org", "team"), ("meera@co.org", "personal")):
        src = tmp_path / f"{scope}.html"
        src.write_text(scope)
        made[scope] = arts.publish(client.path, hub=client.hub, owner=who, owner_account="id-" + who,
                                   source=src, title="Bhava", workflow="program_report", run_id="r",
                                   key="program-bhava", audience="domain", series={"scope": scope})
    team_id, mine_id = made["team"]["series_id"], made["personal"]["series_id"]
    assert "share_url" not in made["personal"]                     # a person's copy starts private
    from hubzoid.artifacts import series
    series.domain_on(client.path, series.get(client.path, mine_id), "meera@co.org")   # she shares it
    data = client.get("/portal/api/reports", params={"hub": client.hub}).json()
    assert data["company"]["on"] is True and [r["company_on"] for r in data["reports"]] == [True, True]
    copy = client.post(f"/portal/api/reports/{team_id}/company", json={"hub": client.hub, "action": "copy"})
    assert copy.json()["url"] == made["team"]["share_url"]
    replaced = client.post(f"/portal/api/reports/{team_id}/company", json={"hub": client.hub, "action": "replace"})
    assert replaced.json()["url"] != made["team"]["share_url"]
    assert client.post(f"/portal/api/reports/{mine_id}/company",
                       json={"hub": client.hub, "action": "copy"}).status_code == 403
    assert client.post(f"/portal/api/reports/{mine_id}/company",
                       json={"hub": client.hub, "action": "off"}).json()["on"] is False


def test_people_see_and_change_only_their_own_schedules_and_reports(client, tmp_path, monkeypatch):
    from hubzoid.access import session
    from hubzoid.workflows import observe

    gs = access.store_for(client.path)
    gs.upsert_identity(email="meera@co.org", owui_id="id-meera")
    for perm in ("use_hub", "wf_program_report", "schedule_reports"):
        gs.grant("meera@co.org", client.hub, perm, actor="t")
    _schedule(client, account="id-meera")
    with client.eng.begin() as conn:                          # this workflow takes no inputs
        conn.execute(text("UPDATE hz_personal_schedules SET inputs='{}'"))
    src = tmp_path / "mine.html"
    src.write_text("mine")
    arts.publish(client.path, hub=client.hub, owner="meera@co.org", owner_account="id-meera", source=src,
                 title="Bhava", workflow="program_report", run_id="ps-ps1-first", key="program-bhava",
                 series={"scope": "personal", "schedule_id": "ps1"})
    monkeypatch.setattr(observe, "schedule_runs", lambda *_a, **_k: [{"status": "SUCCESS"}])
    who = {"v": ("id-meera", "meera@co.org")}
    monkeypatch.setattr(session, "verified_person", lambda *_a, **_k: who["v"])
    my = client.get("/portal/api/my").json()
    row = my["schedules"][0]
    assert row["id"] == "ps1" and row["reports"][0]["url"].endswith("/program-bhava/by/" + row["reports"][0]["url"].rsplit("/", 1)[1])
    assert "owner" not in row and "cost_30d" not in row and my["reports"][0]["title"] == "Bhava"
    assert my["agents"][0]["api"] == "/portal/api"
    hub = {"hub": client.hub}
    assert client.get("/portal/api/my/schedules/ps1/runs", params=hub).json() == {"runs": [{"status": "SUCCESS"}]}
    assert client.post("/portal/api/my/schedules/ps1/pause", json=hub).status_code == 200
    assert client.post("/portal/api/my/schedules/ps1/resume", json=hub).status_code == 200
    # Run now needs the agent's own engine: in a gateway the page sends it to the agent's bridge.
    assert "can't run" in client.post("/portal/api/my/schedules/ps1/run_now", json=hub).json()["detail"]
    import hubzoid.chat.common as chat_common
    monkeypatch.setattr(chat_common, "api_bases", lambda _d: {client.hub: "/b/irs"})
    assert client.get("/portal/api/my").json()["agents"][0]["api"] == "/b/irs/portal/api"
    client.post("/portal/api/schedules/ps1/pause", json=hub)                       # an admin's pause
    refused = client.post("/portal/api/my/schedules/ps1/resume", json=hub)
    assert refused.status_code == 409 and "paused by an admin" in refused.json()["detail"]
    # A new account that took over the email is someone else.
    who["v"] = ("id-new", "meera@co.org")
    my = client.get("/portal/api/my").json()
    assert my["schedules"] == [] and my["reports"] == []
    assert client.post("/portal/api/my/schedules/ps1/stop", json=hub).status_code == 404
    assert client.get("/portal/api/my/schedules/ps1/runs", params=hub).status_code == 404
    who["v"] = None
    assert client.get("/portal/api/my").status_code == 401
