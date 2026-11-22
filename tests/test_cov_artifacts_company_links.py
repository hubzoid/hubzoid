"""Company links and personal report links (hubzoid.artifacts.web): a
company reader gets the newest copy and its download, an API call without a
sign-in is 401, someone who already uses the agent is never marked a reader,
a malformed token gets the one refusal page; the owner turns a personal
report's company link on and off (only the owner, only with the grant, only
when company sign-in is set up); a personal report's link redirects to sign
in and refuses a malformed or unknown reference. Uses the deployment and
viewer of tests/test_report_series.py."""
from __future__ import annotations

from hubzoid.access import store_for
from hubzoid.artifacts import series
from tests.test_report_series import (  # noqa: F401
    MEERA,
    READER,
    SAM,
    TEAM_ACCT,
    _as,
    _company,
    _publish,
    hub,
    web,
)


def _company_report(hub, tmp_path, monkeypatch):  # noqa: F811
    _company(monkeypatch)
    gs = store_for(hub)
    gs.grant(TEAM_ACCT, "reports", "share_domain_links", actor="t")
    out = _publish(hub, tmp_path, audience="domain")
    gs.upsert_identity(email=READER, owui_id="id-" + READER)
    return gs, out, out["share_url"].rsplit("/", 1)[1]


def test_a_company_reader_gets_the_newest_copy_and_its_download(hub, web, tmp_path, monkeypatch):  # noqa: F811
    gs, out, token = _company_report(hub, tmp_path, monkeypatch)
    latest = _as(web, READER).get(f"/portal/r/{token}/api/latest")
    assert latest.status_code == 200 and latest.json()["artifact"] == out["id"]
    down = _as(web, READER).get(f"/portal/r/{token}/download")
    assert down.status_code == 200 and down.content == b"<h1>1</h1>"
    assert "attachment" in down.headers["content-disposition"]
    assert _as(web, None).get(f"/portal/r/{token}/api/latest").status_code == 401
    page = _as(web, READER).get("/portal/r/bad token!")
    assert page.status_code == 404 and "The link may be turned off or incomplete." in page.text


def test_someone_who_uses_the_agent_is_never_marked_a_reader(hub, web, tmp_path, monkeypatch):  # noqa: F811
    gs, _out, token = _company_report(hub, tmp_path, monkeypatch)
    assert _as(web, SAM).get(f"/portal/r/{token}/api/latest").status_code == 200
    assert gs.is_reader(SAM) is False
    assert _as(web, READER).get(f"/portal/r/{token}/api/latest").status_code == 200
    assert gs.is_reader(READER) is True


def test_the_owner_turns_a_personal_reports_company_link_on_and_off(hub, web, tmp_path, monkeypatch):  # noqa: F811
    mine = _publish(hub, tmp_path, owner=MEERA, scope="personal")
    sid = mine["series_id"]
    url = f"/portal/series/api/{sid}/company"
    r = _as(web, MEERA).post(url, json={"action": "on"})
    assert r.status_code == 409                                       # company sign-in not set up
    _company(monkeypatch)
    r = _as(web, MEERA).post(url, json={"action": "on"})
    assert r.status_code == 403 and "An admin can allow it" in r.json()["detail"]
    store_for(hub).grant(MEERA, "reports", "share_domain_links", actor="admin")
    on = _as(web, MEERA).post(url, json={"action": "on"}).json()
    assert on["on"] is True and "/portal/r/" in on["url"] and series.get(hub, sid).domain_on
    assert _as(web, SAM).post(url, json={"action": "off"}).status_code == 404   # not the owner
    assert _as(web, MEERA).post(url, json={"action": "off"}).json() == {"ok": True, "on": False}
    assert not series.get(hub, sid).domain_on
    assert _as(web, MEERA).post(url, json={"action": "maybe"}).status_code == 422


def test_a_personal_reports_link(hub, web, tmp_path):  # noqa: F811
    mine = _publish(hub, tmp_path, owner=MEERA, scope="personal")
    path = mine["latest_url"].split("://", 1)[-1]
    path = path[path.index("/portal/"):]
    assert _as(web, MEERA).get(path).status_code == 200
    signed_out = _as(web, None).get(path)
    assert signed_out.status_code == 302 and signed_out.headers["location"].startswith("/auth?redirect=/portal/latest/")
    assert _as(web, MEERA).get("/portal/latest/reports/program-intro/by/NOT-A-REF").status_code == 404
    gone = _as(web, MEERA).get("/portal/latest/reports/program-intro/by/0123456789ab")
    assert gone.status_code == 404 and f"You are signed in as {MEERA}" in gone.text
