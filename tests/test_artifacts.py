"""Published artifacts: private by default, owner-controlled sharing, public
links that die the moment they are revoked, and no other way in.
No model, no network: the Open WebUI session check is stubbed.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid import _fs, artifacts as arts
from hubzoid.access import store_for

OWNER, TEAMMATE, OUTSIDER, ADMIN = ("priya@company.com", "sam@company.com",
                                    "eve@company.com", "boss@company.com")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for k in ("WEBUI_AUTH", "HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_PUBLIC_URL",
              "WEBUI_URL", "HUBZOID_ARTIFACT_ALLOW_ORIGINS", "HUBZOID_ARTIFACT_LINK_DAYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: sales\n---\nbody")
    gs = store_for(d)
    for e in (OWNER, TEAMMATE, OUTSIDER, ADMIN):
        gs.upsert_identity(email=e, owui_id="id-" + e)
    return d


def _managed(hub):
    gs = store_for(hub)
    gs.set_authoritative(True, hub="sales")
    gs.grant(OWNER, "sales", "use_hub", actor="t")
    gs.grant(TEAMMATE, "sales", "use_hub", actor="t")
    gs.grant(ADMIN, "*", "manage_access", actor="t")   # org admin, no share
    return gs


def _file(tmp_path, name="report.html", body="<h1>Q3</h1>"):
    p = tmp_path / "run" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(body)
    return p


def _publish(hub, tmp_path, name="report.html", body="<h1>Q3</h1>", **kw):
    out = arts.publish(hub, hub="sales", owner=OWNER, owner_account="id-" + OWNER,
                       source=_file(tmp_path, name, body), title="Q3 report", **kw)
    return arts.get(hub, out["id"]), out


# --- publishing ------------------------------------------------------------------

def test_publish_stores_a_private_copy_and_never_overwrites(hub, tmp_path):
    art, out = _publish(hub, tmp_path)
    assert out["url"].endswith(f"/portal/artifacts/{art.id}")
    assert (art.owner, art.audience, art.kind) == (OWNER, "owner", "html")
    stored = arts.content_path(hub, art)
    assert stored.read_text() == "<h1>Q3</h1>"
    assert ".hubzoid/artifacts" in stored.as_posix()
    art2, _ = _publish(hub, tmp_path, body="<h1>Q4</h1>")      # same file name
    assert art2.id != art.id
    assert arts.content_path(hub, art).read_text() == "<h1>Q3</h1>"
    assert arts.content_path(hub, art2).read_text() == "<h1>Q4</h1>"


def test_a_retried_publish_returns_the_first_artifact(hub, tmp_path):
    _, a = _publish(hub, tmp_path, idem_key="run-1:3")
    _, b = _publish(hub, tmp_path, idem_key="run-1:3")
    assert a["id"] == b["id"]


def test_publishing_never_makes_a_public_link(hub, tmp_path):
    with pytest.raises(arts.ArtifactError):
        _publish(hub, tmp_path, audience="link")


def test_publish_refuses_missing_and_oversized_files(hub, tmp_path, monkeypatch):
    with pytest.raises(arts.ArtifactError):
        arts.publish(hub, hub="sales", owner=OWNER, owner_account=None,
                     source=tmp_path / "nope.html")
    monkeypatch.setenv("HUBZOID_ARTIFACT_MAX_BYTES", "4")
    with pytest.raises(arts.ArtifactError):
        _publish(hub, tmp_path)


# --- who may open it -----------------------------------------------------------------

def test_private_by_default_even_for_admins(hub, tmp_path):
    _managed(hub)
    art, _ = _publish(hub, tmp_path)
    assert arts.role(hub, art, OWNER) == "owner"
    for other in (TEAMMATE, OUTSIDER, ADMIN, "", "*"):
        assert arts.role(hub, art, other) is None


def test_hub_audience_follows_current_membership(hub, tmp_path):
    gs = _managed(hub)
    art, _ = _publish(hub, tmp_path)
    arts.set_audience(hub, art, OWNER, "hub")
    art = arts.get(hub, art.id)
    assert arts.role(hub, art, TEAMMATE) == "viewer"
    assert arts.role(hub, art, OUTSIDER) is None
    gs.revoke(TEAMMATE, "sales", "use_hub", actor="t")         # leaves the hub
    assert arts.role(hub, art, TEAMMATE) is None
    gs.grant(TEAMMATE, "sales", "use_hub", actor="t")
    arts.set_audience(hub, art, OWNER, "owner")                  # owner narrows it
    assert arts.role(hub, arts.get(hub, art.id), TEAMMATE) is None


def test_specific_people_and_groups(hub, tmp_path, monkeypatch):
    gs = _managed(hub)
    gs.grant(OUTSIDER, "sales", "use_hub", actor="t")
    art, _ = _publish(hub, tmp_path)
    arts.set_audience(hub, art, OWNER, "people",
                      [{"kind": "user", "principal": TEAMMATE},
                       {"kind": "group", "principal": "Finance"}])
    art = arts.get(hub, art.id)
    assert arts.role(hub, art, TEAMMATE) == "viewer"
    assert arts.role(hub, art, OUTSIDER) is None
    monkeypatch.setattr(arts, "_groups_of", lambda *_: {"finance"})
    assert arts.role(hub, art, OUTSIDER) == "viewer"
    gs.suspend(OUTSIDER, actor="t")                              # blocked account
    assert arts.role(hub, art, OUTSIDER) is None


def test_sharing_checks_eligibility_and_legacy_hubs(hub, tmp_path):
    art, _ = _publish(hub, tmp_path)
    with pytest.raises(arts.ArtifactError) as err:               # legacy: can't verify members
        arts.set_audience(hub, art, OWNER, "hub")
    assert err.value.status == 409
    _managed(hub)
    with pytest.raises(arts.ArtifactError):                      # not a member
        arts.set_audience(hub, art, OWNER, "people", [OUTSIDER])
    with pytest.raises(arts.ArtifactError):                      # only the owner
        arts.set_audience(hub, art, TEAMMATE, "hub")
    with pytest.raises(arts.ArtifactError):
        arts.set_audience(hub, art, ADMIN, "hub")


# --- public links ----------------------------------------------------------------------

def _token(url: str) -> str:
    assert "/portal/p/#" in url
    return url.split("#", 1)[1]


def test_public_links_need_the_permission(hub, tmp_path):
    gs = _managed(hub)
    art, _ = _publish(hub, tmp_path)
    with pytest.raises(arts.ArtifactError) as err:
        arts.create_link(hub, art, OWNER)
    assert err.value.status == 403
    gs.grant(OWNER, "sales", arts.PUBLIC_LINK_PERMISSION, actor="t")
    link = arts.create_link(hub, art, OWNER, days=7)
    found = arts.open_link(hub, _token(link["url"]))
    assert found and found[0].id == art.id
    # Revoking the permission kills the live link at once.
    gs.revoke(OWNER, "sales", arts.PUBLIC_LINK_PERMISSION, actor="t")
    assert arts.open_link(hub, _token(link["url"])) is None


def test_public_link_rotation_revocation_expiry_and_audience(hub, tmp_path, monkeypatch):
    gs = _managed(hub)
    gs.grant(OWNER, "sales", arts.PUBLIC_LINK_PERMISSION, actor="t")
    art, _ = _publish(hub, tmp_path)
    first = _token(arts.create_link(hub, art, OWNER)["url"])
    second = _token(arts.create_link(hub, arts.get(hub, art.id), OWNER)["url"])
    assert arts.open_link(hub, first) is None                    # rotated away
    assert arts.open_link(hub, second) is not None
    arts.revoke_links(hub, arts.get(hub, art.id), OWNER)
    assert arts.open_link(hub, second) is None
    assert arts.get(hub, art.id).audience == "owner"
    third = _token(arts.create_link(hub, arts.get(hub, art.id), OWNER, days=1)["url"])
    real = time.time
    monkeypatch.setattr(arts.time, "time", lambda: real() + 2 * 86400)
    assert arts.open_link(hub, third) is None                    # expired
    monkeypatch.setattr(arts.time, "time", real)
    assert arts.open_link(hub, third) is not None
    arts.set_audience(hub, arts.get(hub, art.id), OWNER, "hub")  # leaving "link" ends it
    assert arts.open_link(hub, third) is None


def test_link_tokens_are_stored_hashed(hub, tmp_path):
    from sqlalchemy import text

    gs = _managed(hub)
    gs.grant(OWNER, "sales", arts.PUBLIC_LINK_PERMISSION, actor="t")
    art, _ = _publish(hub, tmp_path)
    token = _token(arts.create_link(hub, art, OWNER)["url"])
    with gs._engine.connect() as c:
        dump = " ".join(str(v) for r in c.execute(text("SELECT * FROM hz_artifact_links"))
                        for v in r)
    assert token not in dump
    assert len(token) >= 40


def test_delete_removes_the_file_and_every_way_in(hub, tmp_path):
    gs = _managed(hub)
    gs.grant(OWNER, "sales", arts.PUBLIC_LINK_PERMISSION, actor="t")
    art, _ = _publish(hub, tmp_path)
    token = _token(arts.create_link(hub, art, OWNER)["url"])
    stored = arts.content_path(hub, art)
    with pytest.raises(arts.ArtifactError):
        arts.delete(hub, art, TEAMMATE)
    arts.delete(hub, arts.get(hub, art.id), OWNER)
    assert not stored.exists()
    assert arts.get(hub, art.id) is None
    assert arts.open_link(hub, token) is None


# --- no other way in -------------------------------------------------------------------

def test_agent_file_tools_and_legacy_links_cannot_reach_the_store(hub, tmp_path):
    from hubzoid import memory

    art, _ = _publish(hub, tmp_path)
    stored = arts.content_path(hub, art)
    assert _fs.agent_read_refusal(hub, stored)                   # private hub state
    legacy_base = memory.chat_artifact_dir(hub, "any-chat").resolve()
    assert legacy_base not in stored.parents                      # /artifacts/<chat>/<file>


# --- the viewer --------------------------------------------------------------------------

@pytest.fixture
def web(hub, monkeypatch):
    from hubzoid.access import session
    from hubzoid.artifacts import web as artifacts_web

    def fake_verified(request, hub_dir=None):
        token = request.cookies.get("token") or ""
        return token.split(":", 1)[1] if token.startswith("session:") else ""

    monkeypatch.setattr(session, "verified_email", fake_verified)
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


def test_signed_out_viewer_goes_to_sign_in_and_back(hub, web, tmp_path):
    art, _ = _publish(hub, tmp_path)
    r = _as(web, None).get(f"/portal/artifacts/{art.id}?redirect=https://evil.example")
    assert r.status_code == 302
    assert r.headers["location"] == f"/auth?redirect=/portal/artifacts/{art.id}"
    assert _as(web, None).get("/portal/artifacts/not-an-id").status_code == 404
    assert _as(web, None).get(f"/portal/artifacts/api/{art.id}").status_code == 401
    assert _as(web, None).get(f"/portal/artifacts/{art.id}/download").status_code == 401


def test_ordinary_owner_views_and_downloads_without_console_rights(hub, web, tmp_path):
    art, _ = _publish(hub, tmp_path)
    c = _as(web, OWNER)
    page = c.get(f"/portal/artifacts/{art.id}")
    assert page.status_code == 200 and "script-src 'nonce-" in page.headers["content-security-policy"]
    meta = c.get(f"/portal/artifacts/api/{art.id}").json()
    assert meta["role"] == "owner" and meta["sharing"]["audience"] == "owner"
    content = c.get(f"/portal/artifacts/{art.id}/content")
    csp = content.headers["content-security-policy"]
    assert content.text == "<h1>Q3</h1>"
    assert csp.startswith("sandbox ") and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "frame-ancestors 'self'" in csp
    assert content.headers["x-content-type-options"] == "nosniff"
    dl = c.get(f"/portal/artifacts/{art.id}/download")
    assert dl.headers["content-disposition"].startswith("attachment")


def test_others_get_nothing_on_any_route(hub, web, tmp_path):
    _managed(hub)
    art, _ = _publish(hub, tmp_path)
    for who in (TEAMMATE, OUTSIDER, ADMIN):
        c = _as(web, who)
        assert c.get(f"/portal/artifacts/{art.id}").status_code == 404
        for path in ("/content", "/download"):
            assert c.get(f"/portal/artifacts/{art.id}{path}").status_code == 404
        assert c.get(f"/portal/artifacts/api/{art.id}").status_code == 404
        assert c.post(f"/portal/artifacts/api/{art.id}/audience",
                      json={"audience": "hub"}).status_code == 404
        assert c.delete(f"/portal/artifacts/api/{art.id}").status_code == 404


def test_owner_shares_and_revokes_through_the_viewer(hub, web, tmp_path):
    _managed(hub)
    art, _ = _publish(hub, tmp_path)
    owner = _as(web, OWNER)
    r = owner.post(f"/portal/artifacts/api/{art.id}/audience",
                   json={"audience": "people", "people": [{"kind": "user", "principal": TEAMMATE}]})
    assert r.status_code == 200
    assert _as(web, TEAMMATE).get(f"/portal/artifacts/api/{art.id}").json()["role"] == "viewer"
    viewer_meta = _as(web, TEAMMATE).get(f"/portal/artifacts/api/{art.id}").json()
    assert "sharing" not in viewer_meta                            # viewers can't manage
    assert _as(web, TEAMMATE).delete(f"/portal/artifacts/api/{art.id}").status_code == 404
    owner = _as(web, OWNER)
    owner.post(f"/portal/artifacts/api/{art.id}/audience", json={"audience": "owner"})
    assert _as(web, TEAMMATE).get(f"/portal/artifacts/{art.id}/content").status_code == 404


def test_mutations_must_come_from_the_same_origin(hub, web, tmp_path):
    _managed(hub)
    art, _ = _publish(hub, tmp_path)
    c = _as(web, OWNER)
    r = c.post(f"/portal/artifacts/api/{art.id}/audience", json={"audience": "hub"},
               headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_public_link_flow_keeps_the_token_out_of_urls(hub, web, tmp_path):
    gs = _managed(hub)
    gs.grant(OWNER, "sales", arts.PUBLIC_LINK_PERMISSION, actor="t")
    art, _ = _publish(hub, tmp_path)
    created = _as(web, OWNER).post(f"/portal/artifacts/api/{art.id}/link",
                                   json={"action": "create", "days": 1}).json()
    token = _token(created["url"])
    anon = _as(web, None)
    page = anon.get("/portal/p/")
    assert page.status_code == 200 and token not in page.text
    assert anon.get(f"/portal/p/{art.id}/content").status_code == 404   # no cookie yet
    opened = anon.post("/portal/p/open", json={"token": token})
    assert opened.status_code == 200 and opened.json()["id"] == art.id
    cookie = opened.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/portal/p/" in cookie and "SameSite=strict" in cookie
    assert token not in cookie
    assert anon.get(f"/portal/p/{art.id}/content").text == "<h1>Q3</h1>"
    # The owner turns it off: the same browser loses access immediately.
    _as(web, OWNER).post(f"/portal/artifacts/api/{art.id}/link", json={"action": "revoke"})
    anon.cookies.set(f"hz_pl_{art.id}", cookie.split("=", 1)[1].split(";", 1)[0],
                     path="/portal/p/")
    assert anon.get(f"/portal/p/{art.id}/content").status_code == 404
    assert anon.post("/portal/p/open", json={"token": token}).status_code == 404


def test_wrong_or_missing_public_token(hub, web):
    anon = _as(web, None)
    assert anon.post("/portal/p/open", json={"token": "x" * 43}).status_code == 404
    assert anon.post("/portal/p/open", json={"token": "short"}).status_code == 422


@pytest.mark.parametrize("name,body,kind,disposition,ctype", [
    ("chart.png", "\x89PNG", "image", "inline", "image/png"),
    ("chart.svg", "<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
     "svg", "inline", "image/svg+xml"),
    ("data.csv", "a,b\n1,2\n", "csv", "inline", "text/plain"),
    ("q3.pdf", "%PDF-1.4", "pdf", "inline", "application/pdf"),
    ("tool.js", "alert(1)", "download", "attachment", ""),
    ("page.xhtml", "<html/>", "download", "attachment", ""),
])
def test_each_file_type_is_served_safely(hub, web, tmp_path, name, body, kind, disposition, ctype):
    art, _ = _publish(hub, tmp_path, name=name, body=body)
    assert art.kind == kind
    r = _as(web, OWNER).get(f"/portal/artifacts/{art.id}/content")
    assert r.headers["content-disposition"].startswith(disposition)
    assert r.headers["x-content-type-options"] == "nosniff"
    if ctype:
        assert r.headers["content-type"].startswith(ctype)
    if kind != "pdf":
        assert r.headers["content-security-policy"].startswith("sandbox")
    meta = _as(web, OWNER).get(f"/portal/artifacts/api/{art.id}").json()
    if kind == "csv":
        assert meta["preview"]["rows"] == [["a", "b"], ["1", "2"]]


def test_extra_origins_must_be_https(monkeypatch):
    from hubzoid.artifacts.web import html_csp

    monkeypatch.setenv("HUBZOID_ARTIFACT_ALLOW_ORIGINS",
                       "https://cdn.jsdelivr.net, http://evil.example, javascript:x")
    csp = html_csp()
    assert "https://cdn.jsdelivr.net" in csp and "evil.example" not in csp
    assert "javascript" not in csp


def test_permission_catalog_lists_public_links(hub):
    from hubzoid import deployment

    perms = {p["permission"]: p for p in deployment.permission_catalog(hub)}
    assert perms[arts.PUBLIC_LINK_PERMISSION]["sensitive"] is True
