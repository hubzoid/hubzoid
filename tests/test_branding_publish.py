"""Customer branding: validation, safe assets, reviewed activation and
rollback (hubzoid.ui.branding_*), through the service, the API and the CLI."""
from __future__ import annotations

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from typer.testing import CliRunner

from hubzoid.ui import branding_assets, branding_css, branding_models
from hubzoid.ui.branding_models import BrandError
from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
       b'<rect width="10" height="10" fill="#B5471F"/></svg>')


def png(size=(64, 64), colour=(181, 71, 31, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, colour).save(out, format="PNG")
    return out.getvalue()


CONF = {"version": 1, "identity": {"name": "Example Operations", "short_name": "Example"},
        "assets": {"logo_light": "branding/logo.svg", "favicon": "branding/favicon.png"},
        "copy": {"en": {"welcome_title": "Your team's workspace", "signin_title": "Welcome"}},
        "links": {"help": "https://example.invalid/help"}}
FILES = {"branding/logo.svg": SVG, "branding/favicon.png": png()}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    return TestClient(build_app())


def b64(files):
    return {k: base64.b64encode(v).decode() for k, v in files.items()}


def publish(client, conf=CONF, files=FILES, theme=None, generation=0, key=None):
    headers = dict(ORIGIN)
    d = client.post("/api/branding/drafts", headers=headers,
                    json={"configuration": conf, "theme": theme, "files": b64(files)})
    assert d.status_code == 201, d.text
    d = d.json()
    r = client.post(f"/api/branding/drafts/{d['id']}/reviews", headers=headers,
                    json={"digest": d["digest"], "expected_revision": d["revision"], "decision": "approve"})
    assert r.status_code == 201, r.text
    h = dict(headers, **({"Idempotency-Key": key} if key else {}))
    a = client.post(f"/api/branding/drafts/{d['id']}/activate", headers=h,
                    json={"review_id": r.json()["review_id"], "expected_revision": generation})
    return d, r.json(), a


# -- validation ------------------------------------------------------------------
def test_optional_file_defaults_and_unknown_keys():
    assert branding_models.default()["identity"]["name"] == "Hubzoid"
    with pytest.raises(BrandError) as exc:
        branding_models.validate({"version": 1, "identity": {"nmae": "x"}})
    assert exc.value.code == "unknown_key" and exc.value.extra["pointer"] == "/identity/nmae"
    with pytest.raises(BrandError):
        branding_models.validate({"version": 1, "copy": {"ar": {"welcome_title": "x"}}})  # English only
    with pytest.raises(BrandError):
        branding_models.validate({"version": 1, "legal": {"require_acceptance": True}})
    with pytest.raises(BrandError):
        branding_models.validate({"version": 1, "links": {"help": "javascript:alert(1)"}})


@pytest.mark.parametrize("svg", [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect onload="x()"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink">'
    b'<image x:href="https://evil.invalid/a.png"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(https://evil.invalid/#a)"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
    b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
])
def test_svg_external_reference_and_script_rejected(svg):
    with pytest.raises(BrandError):
        branding_assets.check("logo_light", svg, "branding/logo.svg")


def test_raster_reencoded_and_bounded():
    checked = branding_assets.check("favicon", png(), "branding/favicon.png")
    assert checked.media_type == "image/png" and checked.width == 64
    with pytest.raises(BrandError):
        branding_assets.check("icon_192", png((100, 100)), "branding/icon.png")  # exact size
    gif = io.BytesIO()
    Image.new("RGB", (4, 4)).save(gif, format="GIF")
    with pytest.raises(BrandError):
        branding_assets.check("favicon", gif.getvalue(), "branding/f.gif")


def test_css_cannot_hide_approval_or_escape_slots():
    ok = branding_css.parse('[data-brand-slot="brand.welcome"] { background-color: #FFF5DA; padding: 8px 12px; }')
    assert ok == {"brand.welcome": {"background-color": "#FFF5DA", "padding": "8px 12px"}}
    for bad in ('[data-brand-slot="brand.welcome"] { display: none; }',
                '.approval-card { visibility: hidden; }',
                '[data-brand-slot="brand.welcome"] button { color: #000000; }',
                '@import url(https://evil.invalid/x.css);',
                '[data-brand-slot="brand.footer"] { background-color: url(x); }'):
        with pytest.raises(BrandError) as exc:
            branding_css.parse(bad)
        assert exc.value.code == "unsafe_style"


# -- publication -------------------------------------------------------------------
def test_hash_covers_assets_and_generated_icons(client):
    d1 = client.post("/api/branding/drafts", headers=ORIGIN,
                     json={"configuration": CONF, "files": b64(FILES)}).json()
    other = dict(FILES, **{"branding/favicon.png": png(colour=(0, 0, 0, 255))})
    d2 = client.post("/api/branding/drafts", headers=ORIGIN,
                     json={"configuration": CONF, "files": b64(other)}).json()
    assert d1["state"] == "valid" and d1["digest"] != d2["digest"]
    generated = {a["key"]: a for a in d1["assets"] if a.get("generated")}
    assert set(generated) == {"icon_192", "icon_512", "touch_icon"}
    assert all(a["media_type"] == "image/png" for a in generated.values())


def test_missing_asset_keeps_draft_unreviewable(client):
    d = client.post("/api/branding/drafts", headers=ORIGIN,
                    json={"configuration": CONF, "files": b64({"branding/logo.svg": SVG})}).json()
    assert d["state"] == "draft" and d["validation"]["problems"][0]["code"] == "missing_asset"
    r = client.post(f"/api/branding/drafts/{d['id']}/reviews", headers=ORIGIN,
                    json={"digest": d["digest"], "expected_revision": 1, "decision": "approve"})
    assert r.status_code == 422
    up = client.put(f"/api/branding/drafts/{d['id']}/assets?path=branding/favicon.png", headers=ORIGIN,
                    content=png())
    assert up.status_code == 200 and up.json()["state"] == "valid" and up.json()["revision"] == 2


def test_unreadable_palette_fails_visibly(client):
    d = client.post("/api/branding/drafts", headers=ORIGIN, json={
        "configuration": CONF, "files": b64(FILES),
        "theme": {"version": 1, "palettes": {"light": {"action": "#FFE0D0"}}}}).json()
    assert d["state"] == "draft"
    problem = d["validation"]["problems"][0]
    assert problem["code"] == "contrast_failure" and problem["failures"][0]["mode"] == "light"


def test_activate_then_public_manifest_and_assets(client):
    before = client.get("/api/branding").json()
    assert before["manifest"] is None and before["custom_css_url"] is None
    d, _review, a = publish(client)
    assert a.status_code == 200, a.text
    body = client.get("/api/branding").json()
    assert body["name"] == "Example Operations" and body["manifest"]["generation"] == 1
    logo = client.get(body["logo_url"])
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/svg+xml"
    assert "immutable" in logo.headers["cache-control"]
    theme = client.get("/api/chat/theme").json()
    assert theme["revision"] == a.json()["revision"]


def test_changed_candidate_invalidates_review(client):
    d = client.post("/api/branding/drafts", headers=ORIGIN,
                    json={"configuration": CONF, "files": b64(FILES)}).json()
    review = client.post(f"/api/branding/drafts/{d['id']}/reviews", headers=ORIGIN,
                         json={"digest": d["digest"], "expected_revision": 1, "decision": "approve"}).json()
    changed = dict(CONF, identity={"name": "Changed"})
    client.patch(f"/api/branding/drafts/{d['id']}", headers=ORIGIN,
                 json={"configuration": changed, "expected_revision": 1})
    a = client.post(f"/api/branding/drafts/{d['id']}/activate", headers=ORIGIN,
                    json={"review_id": review["review_id"], "expected_revision": 0})
    assert a.status_code == 409 and a.json()["detail"]["code"] == "revision_conflict"


def test_retry_gives_one_activation_and_stale_generation_conflicts(client):
    d, review, a = publish(client, key="act-1")
    again = client.post(f"/api/branding/drafts/{d['id']}/activate",
                        headers=dict(ORIGIN, **{"Idempotency-Key": "act-1"}),
                        json={"review_id": review["review_id"], "expected_revision": 0})
    assert again.json()["activation_id"] == a.json()["activation_id"]
    assert len(client.get("/api/branding/history").json()["items"]) == 1
    _d2, _r2, stale = publish(client, conf=dict(CONF, identity={"name": "Second"}), generation=0)
    assert stale.status_code == 409


def test_rollback_records_new_activation(client):
    _d, _r, first = publish(client)
    _d2, _r2, second = publish(client, conf=dict(CONF, identity={"name": "Second"}), generation=1)
    assert client.get("/api/branding").json()["name"] == "Second"
    rb = client.post("/api/branding/rollbacks", headers=ORIGIN,
                     json={"target_revision": first.json()["revision"]}).json()
    review = client.post(f"/api/branding/drafts/{rb['id']}/reviews", headers=ORIGIN,
                         json={"digest": rb["digest"], "expected_revision": 1, "decision": "approve"}).json()
    act = client.post(f"/api/branding/drafts/{rb['id']}/activate", headers=ORIGIN,
                      json={"review_id": review["review_id"], "expected_revision": 2})
    assert act.status_code == 200, act.text
    history = client.get("/api/branding/history").json()["items"]
    assert [h["generation"] for h in history] == [3, 2, 1]
    assert [h["kind"] for h in history] == ["rollback", "activate", "activate"]
    assert client.get("/api/branding").json()["name"] == "Example Operations"


def test_preview_private_and_teammate_denied(client, monkeypatch):
    d = client.post("/api/branding/drafts", headers=ORIGIN,
                    json={"configuration": CONF, "files": b64(FILES)}).json()
    p = client.get(f"/api/branding/drafts/{d['id']}/preview")
    assert p.status_code == 200 and p.headers["cache-control"] == "no-store"
    digest = p.json()["assets"]["logo_light"].rsplit("/", 1)[1]
    # a draft-only file is never public
    assert client.get(f"/branding/assets/x/{digest.split(':')[1]}/logo.svg").status_code == 404
    as_people(monkeypatch)
    assert client.get(f"/api/branding/drafts/{d['id']}/preview", headers=who("ana")).status_code == 403
    assert client.post("/api/branding/drafts", headers=who("ana"),
                       json={"configuration": CONF}).status_code == 403


def test_terms_bind_person_and_policy(client):
    conf = dict(CONF, legal={"version": "2026-10", "require_acceptance": True},
                links={"terms": "https://example.invalid/terms"})
    publish(client, conf=conf)
    me = client.get("/api/branding/me").json()
    assert me["terms_required"] is True
    wrong = client.post("/api/branding/me/legal-acceptances", headers=ORIGIN,
                        json={"version": "2026-10", "policy_hash": "sha256:00"})
    assert wrong.status_code == 409
    from tests.chat_helpers import chat_body

    agent = client.get("/api/agents").json()["default_agent"]
    blocked = client.post("/api/chat", headers=ORIGIN,
                          json=chat_body("c_terms_check_1", "hi", agent=agent, message_id="m_terms_00001"))
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "terms_required"
    ok = client.post("/api/branding/me/legal-acceptances", headers=ORIGIN,
                     json={"version": "2026-10", "policy_hash": me["policy_hash"]})
    assert ok.status_code == 200 and client.get("/api/branding/me").json()["terms_required"] is False


def test_legacy_source_explicit_and_custom_css_dropped(hub, client):
    (hub / "branding" / "custom.css").write_text("body { display: none }")
    from hubzoid.cli import app as cli_app

    out = CliRunner().invoke(cli_app, ["branding", "inspect", str(hub), "--json"])
    assert out.exit_code == 0, out.output
    report = json.loads(out.output)["legacy"]
    assert {"file": "custom.css"}.items() <= next(r for r in report if r["file"] == "custom.css").items()
    assert next(r for r in report if r["file"] == "custom.css")["disposition"] == "dropped"
    assert client.get("/branding/custom.css").status_code == 404


def test_cli_journey_matches_api(hub, client):
    from hubzoid.cli import app as cli_app

    (hub / "branding" / "logo.svg").write_bytes(SVG)
    (hub / "branding" / "favicon.png").write_bytes(png())
    (hub / "branding.yaml").write_text(json.dumps(CONF))  # JSON is valid YAML
    run = CliRunner().invoke
    assert run(cli_app, ["branding", "validate", str(hub), "--json"]).exit_code == 0
    draft = json.loads(run(cli_app, ["branding", "draft", "create", str(hub), "--json"]).output)
    review = json.loads(run(cli_app, ["branding", "review", draft["id"], str(hub), "--digest", draft["digest"],
                                      "--expected-revision", "1", "--decision", "approve", "--json"]).output)
    act = run(cli_app, ["branding", "activate", draft["id"], str(hub), "--review", review["review_id"],
                        "--expected-revision", "0", "--json"])
    assert act.exit_code == 0, act.output
    assert client.get("/api/branding").json()["name"] == "Example Operations"
    stale = run(cli_app, ["branding", "activate", draft["id"], str(hub), "--review", review["review_id"],
                          "--expected-revision", "0", "--json"])
    assert stale.exit_code == 4


def test_decorative_background_must_work_in_every_mode(client):
    css = b'[data-brand-slot="brand.welcome"] { background-color: #EAF2F8; padding: 8px; }'
    conf = dict(CONF, presentation={"decorative_css": "branding/deco.css"})
    d = client.post("/api/branding/drafts", headers=ORIGIN,
                    json={"configuration": conf, "files": b64({**FILES, "branding/deco.css": css})}).json()
    assert d["state"] == "draft"
    p = d["validation"]["problems"][0]
    assert p["code"] == "contrast_failure" and p["failures"][0]["mode"] == "dark"
    ok_css = b'[data-brand-slot="brand.welcome"] { border-color: #1F5F8B; padding: 8px; }'
    d2 = client.post("/api/branding/drafts", headers=ORIGIN,
                     json={"configuration": conf, "files": b64({**FILES, "branding/deco.css": ok_css})}).json()
    assert d2["state"] == "valid"


def test_changed_validator_needs_revalidation_then_review(client, hub):
    from hubzoid.ui import branding_store

    d = client.post("/api/branding/drafts", headers=ORIGIN, json={"configuration": CONF, "files": b64(FILES)}).json()
    svc = branding_store.service_for(hub)
    with svc.engine.begin() as conn:  # as if made by an earlier release's checks
        conn.execute(branding_store.drafts.update().values(validator_version="branding-v0"))
    r = client.post(f"/api/branding/drafts/{d['id']}/reviews", headers=ORIGIN,
                    json={"digest": d["digest"], "expected_revision": 1, "decision": "approve"})
    assert r.status_code == 409 and "Revalidate" in r.json()["detail"]["message"]
    again = client.post(f"/api/branding/drafts/{d['id']}/validate", headers=ORIGIN).json()
    assert again["revision"] == 2
    ok = client.post(f"/api/branding/drafts/{d['id']}/reviews", headers=ORIGIN,
                     json={"digest": again["digest"], "expected_revision": 2, "decision": "approve"})
    assert ok.status_code == 201
