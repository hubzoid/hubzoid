"""Report series: one live link per report, team or personal, with company
links for readers who don't use the agent. No model, no engine."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid import artifacts as arts
from hubzoid.access import store_for
from hubzoid.artifacts import series

TEAM_ACCT, MEERA, SAM, READER, OUTSIDER = ("reports@co.org", "meera@co.org", "sam@co.org",
                                           "asha@co.org", "eve@gmail.com")
COMPANY_ENV = {"HUBZOID_SHARE_DOMAINS": "co.org", "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "s",
               "ENABLE_OAUTH_SIGNUP": "true", "OAUTH_ALLOWED_DOMAINS": "co.org"}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for k in ("WEBUI_AUTH", "HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_PUBLIC_URL", "WEBUI_URL",
              *COMPANY_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    d = tmp_path / "reports"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: reports\n---\nbody")
    gs = store_for(d)
    for e in (TEAM_ACCT, MEERA, SAM):
        gs.upsert_identity(email=e, owui_id="id-" + e)
        gs.grant(e, "reports", "use_hub", actor="t")
    return d


def _company(monkeypatch):
    for k, v in COMPANY_ENV.items():
        monkeypatch.setenv(k, v)


def _publish(hub, tmp_path, *, owner=TEAM_ACCT, scope="team", body="<h1>1</h1>", key="program-intro",
             workflow="program_report", run="r1", **kw):
    src = tmp_path / f"{run}-{key}.html"
    src.write_text(body)
    return arts.publish(hub, hub="reports", owner=owner, owner_account="id-" + owner, source=src,
                        title="Intro report", workflow=workflow, run_id=run, key=key,
                        series={"scope": scope, "inputs": {"program": "Intro"}}, **kw)


def test_team_and_personal_series_are_separate_and_unchanged_copies_store_nothing(hub, tmp_path):
    team = _publish(hub, tmp_path, audience="hub")
    mine = _publish(hub, tmp_path, owner=MEERA, scope="personal", run="r2")
    assert team["latest_url"].endswith("/portal/latest/reports/program-intro")
    assert "/by/" in mine["latest_url"] and team["series_id"] != mine["series_id"]
    again = _publish(hub, tmp_path, run="r3", audience="hub")
    assert again["unchanged"] and again["id"] == team["id"]
    assert series.get(hub, team["series_id"]).last_run == "r3"          # touched, not stored
    newer = _publish(hub, tmp_path, run="r4", body="<h1>2</h1>", audience="hub")
    assert newer["id"] != team["id"] and not newer.get("unchanged")
    same_data = _publish(hub, tmp_path, run="r5", body="<h1>3 at 10:05</h1>", digest="d1", audience="hub")
    later = _publish(hub, tmp_path, run="r6", body="<h1>3 at 10:20</h1>", digest="d1", audience="hub")
    assert later["unchanged"] and later["id"] == same_data["id"]        # digest wins over the bytes


def test_a_key_belongs_to_one_workflow(hub, tmp_path):
    _publish(hub, tmp_path)
    with pytest.raises(arts.ArtifactError, match="belongs to workflow program_report"):
        _publish(hub, tmp_path, workflow="other_report", run="r9")


def test_only_a_scheduled_run_decides_what_stopped_updating(hub, tmp_path):
    a = _publish(hub, tmp_path, key="event-1", run="s1")
    _publish(hub, tmp_path, key="event-2", run="s1")
    series.finish_run(hub, "reports", "program_report", "s1", trigger="schedule", scope="team",
                      schedule_id=None, ok=True)
    _publish(hub, tmp_path, key="event-1", run="s2", body="<h1>changed</h1>")
    series.finish_run(hub, "reports", "program_report", "s2", trigger="operator", scope="team",
                      schedule_id=None, ok=True)                         # a one-off run: no verdict
    assert series.team(hub, "reports", "event-2").live == 1
    series.finish_run(hub, "reports", "program_report", "s2", trigger="schedule", scope="team",
                      schedule_id=None, ok=True)
    assert series.team(hub, "reports", "event-2").live == 0                  # event closed
    assert series.state(hub, series.team(hub, "reports", "event-2"))["state"] == "stopped"
    series.finish_run(hub, "reports", "program_report", "s3", trigger="schedule", scope="team",
                      schedule_id=None, ok=False)
    assert series.state(hub, series.get(hub, a["series_id"]))["state"] == "failed"


def test_series_keep_prunes_only_old_copies_of_that_series(hub, tmp_path):
    (hub / "workflows").mkdir()
    (hub / "workflows" / "settings.yaml").write_text("series_keep: 2\n")
    ids = [_publish(hub, tmp_path, run=f"k{i}", body=f"<h1>{i}</h1>")["id"] for i in range(4)]
    assert [arts.get(hub, i) is not None for i in ids] == [False, False, True, True]


def test_domain_publish_needs_company_sign_in_and_the_grant(hub, tmp_path, monkeypatch):
    with pytest.raises(arts.ArtifactError, match="Company links are off"):
        _publish(hub, tmp_path, audience="domain")
    _company(monkeypatch)
    with pytest.raises(arts.ArtifactError, match="can't share reports with co.org"):
        _publish(hub, tmp_path, audience="domain")
    store_for(hub).grant(TEAM_ACCT, "reports", "share_domain_links", actor="t")
    out = _publish(hub, tmp_path, audience="domain")
    assert "/portal/r/" in out["share_url"]
    found = series.get(hub, out["series_id"])
    token = series.domain_token(hub, found)
    assert series.for_token(hub, token).id == found.id
    newer = series.domain_replace(hub, found, "admin@co.org")
    assert series.for_token(hub, token) is None and series.for_token(hub, newer) is not None
    series.domain_off(hub, series.get(hub, found.id), "admin@co.org")
    _publish(hub, tmp_path, run="r8", body="<h1>8</h1>", audience="domain")
    assert series.domain_token(hub, series.get(hub, found.id)) is None   # an admin's "off" sticks
    revived = series.domain_on(hub, series.get(hub, found.id), "admin@co.org")
    assert revived != newer                                              # never the old link back
    _publish(hub, tmp_path, run="r9", body="<h1>8</h1>", audience="hub")
    assert series.domain_token(hub, series.get(hub, found.id)) is None   # the code took it away
    from sqlalchemy import text
    with store_for(hub)._engine.begin() as c:   # an "everyone signed in" grant from an older deployment
        c.execute(text("INSERT INTO hz_grants (subject, hub, permission, created) VALUES ('*', 'reports', 'use_hub', 0)"))
    with pytest.raises(arts.ArtifactError, match="everyone signed in"):
        _publish(hub, tmp_path, run="r10", audience="domain")


# ---- the viewer and company links ----------------------------------------------------------

@pytest.fixture
def web(hub, monkeypatch):
    from hubzoid.access import session
    from hubzoid.artifacts import web as artifacts_web

    monkeypatch.setattr(session, "verified_email",
                        lambda request, hub_dir=None: (request.cookies.get("token") or "").removeprefix("session:"))
    app = FastAPI()
    app.include_router(artifacts_web.build_router(hub))
    client = TestClient(app, follow_redirects=False)
    client.headers.update({"origin": "http://testserver"})
    return client


def _as(client, email):
    client.cookies.clear()
    if email:
        client.cookies.set("token", f"session:{email}")
    return client


def test_the_latest_link_is_the_page_and_reports_its_state(hub, web, tmp_path):
    out = _publish(hub, tmp_path, audience="hub")
    r = _as(web, SAM).get("/portal/latest/reports/program-intro")
    assert r.status_code == 200 and "series" in r.text                   # served, not redirected
    api = _as(web, SAM).get(f"/portal/series/api/{out['series_id']}").json()
    assert api["series"]["scope"] == "team" and api["series"]["state"] == "live"
    assert api["series"]["can_update"] is False                          # not an operator
    latest = _as(web, SAM).get(f"/portal/series/api/{out['series_id']}/latest")
    assert latest.json()["artifact"] == out["id"]
    unchanged = _as(web, SAM).get(f"/portal/series/api/{out['series_id']}/latest",
                                  headers={"If-None-Match": latest.headers["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.content == b""  # HTTP forbids a body; Uvicorn rejects JSON null on 304.
    assert unchanged.headers["etag"] == latest.headers["etag"]


def test_update_now_is_for_the_owner_or_operators_and_waits_between_updates(hub, web, tmp_path, monkeypatch):
    from hubzoid.artifacts import web as artifacts_web

    started = []
    monkeypatch.setattr(artifacts_web, "_start_update", lambda hub_dir, found, subject: started.append(subject) or "run-x")
    mine = _publish(hub, tmp_path, owner=MEERA, scope="personal")
    sid = mine["series_id"]
    assert _as(web, MEERA).post(f"/portal/series/api/{sid}/update").status_code == 429   # just updated
    with series._engine(hub).begin() as c:
        from sqlalchemy import text
        c.execute(text("UPDATE hz_report_series SET last_touch=:t"), {"t": time.time() - 3600})
    assert _as(web, MEERA).post(f"/portal/series/api/{sid}/update").json()["run_id"] == "run-x"
    assert _as(web, SAM).post(f"/portal/series/api/{sid}/update").status_code == 404     # can't see it
    assert started == [MEERA]


def test_company_links_admit_only_the_company_domain_with_one_refusal_page(hub, web, tmp_path, monkeypatch):
    _company(monkeypatch)
    gs = store_for(hub)
    gs.grant(TEAM_ACCT, "reports", "share_domain_links", actor="t")
    out = _publish(hub, tmp_path, audience="domain")
    token = out["share_url"].rsplit("/", 1)[1]
    assert _as(web, None).get(f"/portal/r/{token}").status_code == 302              # sign in first
    for who, bad in ((OUTSIDER, token), (READER, "x" * 16)):
        gs.upsert_identity(email=who, owui_id="id-" + who)
        page = _as(web, who).get(f"/portal/r/{bad}")
        assert page.status_code == 404 and "isn't available" in page.text            # same page both ways
    gs.upsert_identity(email=READER, owui_id="id-" + READER)
    assert _as(web, READER).get(f"/portal/r/{token}").status_code == 200
    meta = _as(web, READER).get(f"/portal/r/{token}/api").json()
    assert meta["content_url"].startswith(f"/portal/r/{token}/content/")
    assert "can_update" not in meta["series"] and "company" not in meta["series"]
    assert _as(web, READER).get(meta["content_url"]).status_code == 200
    assert gs.is_reader(READER)
    assert _as(web, READER).get(f"/portal/latest/reports/program-intro").status_code == 404  # no agent access
    gs.revoke(TEAM_ACCT, "reports", "share_domain_links", actor="admin")
    assert _as(web, READER).get(f"/portal/r/{token}/api").status_code == 404      # sharing taken away
    gs.grant(TEAM_ACCT, "reports", "share_domain_links", actor="admin")
    series.domain_off(hub, series.get(hub, out["series_id"]), "admin@co.org")
    assert _as(web, READER).get(f"/portal/r/{token}/api").status_code == 404


def test_readers_never_inherit_everyone_grants(hub):
    from sqlalchemy import text

    gs = store_for(hub)
    gs.upsert_identity(email=OUTSIDER, owui_id="id-" + OUTSIDER)
    with gs._engine.begin() as c:   # an "everyone signed in" grant from an older deployment
        c.execute(text("INSERT INTO hz_grants (subject, hub, permission, created) VALUES ('*', 'reports', 'use_hub', 0)"))
        c.execute(text("UPDATE hz_identities SET created=0 WHERE subject=:s"), {"s": OUTSIDER})
    gs._rev = None
    gs.upsert_identity(email=READER, owui_id="id-" + READER)
    assert gs.can(READER, "reports", "use_hub")
    assert not gs.mark_reader_if_new(SAM) and not gs.mark_reader_if_new(OUTSIDER)   # a grant; an older account
    assert gs.mark_reader_if_new(READER)
    assert not gs.can(READER, "reports", "use_hub") and "use_hub" not in gs.permissions_for(READER, "reports")
    gs.grant(READER, "reports", "use_hub", actor="admin")
    assert gs.can(READER, "reports", "use_hub")                              # a named grant still works


def test_unchanged_report_restricts_audience(hub, tmp_path):
    first = _publish(hub, tmp_path, audience='hub')
    assert arts.role(hub, arts.get(hub, first['id']), MEERA) == 'viewer'
    again = _publish(hub, tmp_path, run='private', audience='owner')
    assert arts.role(hub, arts.get(hub, again['id']), MEERA) is None, 'Latest report remains shared after publish requested owner-only'


def test_unchanged_report_replaces_named_readers(hub, tmp_path):
    first = _publish(hub, tmp_path, audience='people', share_with=[MEERA])
    again = _publish(hub, tmp_path, run='new-readers', audience='people', share_with=[SAM])
    art = arts.get(hub, again['id'])
    assert arts.role(hub, art, MEERA) is None
    assert arts.role(hub, art, SAM) == 'viewer'



@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=pytest.mark.slow)])
def test_concurrent_claims_cannot_take_a_key_in_different_namespaces(hub, backend, request, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    if backend == "postgres":
        monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", request.getfixturevalue("postgres_url"))
    series._engine(hub)  # migrate before exercising simultaneous claims
    ready = Barrier(2)

    def claim(name):
        ready.wait(timeout=5)
        try:
            return series.claim(hub, hub="reports", workflow=name, scope="personal",
                                owner=name + "@co.org", key="shared-key", title=name).workflow
        except arts.ArtifactError as exc:
            assert exc.status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["one", "two"]))
    assert sum(r is not None for r in results) == 1
