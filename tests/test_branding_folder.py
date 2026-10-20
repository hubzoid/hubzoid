"""Customer branding from its one home, the deployment folder: validation,
safe assets, the public manifest, terms, read-only Settings
(hubzoid.ui.branding_*), through the service, the API and the CLI."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

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


def publish(client=None, conf=CONF, files=FILES, theme=None):
    """Write the brand into the deployment folder (here the hub, with its own
    hubzoid.yaml): the only way a brand changes."""
    hub = Path(os.environ["HUBZOID_HUB_DIR"])
    import yaml

    brand = {k: v for k, v in conf.items() if k != "version"}
    (hub / "hubzoid.yaml").write_text(yaml.safe_dump({"version": 1, "hubs": ["."], "brand": brand}))
    for rel, data in files.items():
        (hub / rel).parent.mkdir(parents=True, exist_ok=True)
        (hub / rel).write_bytes(data)
    if theme is not None:
        (hub / "branding" / "theme.json").write_text(json.dumps(theme))


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
def test_activate_then_public_manifest_and_assets(client):
    before = client.get("/api/branding").json()
    assert before["manifest"] is None and before["custom_css_url"] is None
    publish(client)
    body = client.get("/api/branding").json()
    assert body["name"] == "Example Operations" and body["manifest"]["generation"] == 1
    logo = client.get(body["logo_url"])
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/svg+xml"
    assert "immutable" in logo.headers["cache-control"]
    assert body["manifest"]["assets"]["icon_192"]  # generated from the raster favicon


def test_install_manifest_follows_active_brand(client):
    assert client.get("/manifest.webmanifest").json()["name"] == "Hubzoid"
    publish(client)
    m = client.get("/manifest.webmanifest").json()
    assert m["name"] == "Example Operations" and m["display"] == "standalone" and m["theme_color"].startswith("#")


def test_legacy_folder_serves_only_logo_and_icon(hub, client):
    (hub / "branding" / "page.html").write_text("<script>alert(1)</script>")
    (hub / "branding" / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert client.get("/branding/page.html").status_code == 404
    logo = client.get("/branding/logo.svg")
    assert logo.status_code == 200 and "sandbox" in logo.headers["content-security-policy"]
    publish(client)
    # The hub's own logo stays its agent's avatar; other files stay private.
    assert client.get("/branding/logo.svg").status_code == 200
    assert client.get("/branding/page.html").status_code == 404


def test_terms_check_fails_closed(client, monkeypatch):
    from hubzoid.ui import branding_store
    from tests.chat_helpers import chat_body

    def broken(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(branding_store.BrandingService, "me", broken)
    agent = client.get("/api/agents").json()["default_agent"]
    r = client.post("/api/chat", headers=ORIGIN, json=chat_body("c_terms_down_1", "hi", agent=agent,
                                                               message_id="m_terms_down1"))
    assert r.status_code == 503


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


def test_the_folder_is_the_one_home_and_settings_is_read_only(hub, client):
    gone = client.post("/api/branding/drafts", headers=ORIGIN, json={"configuration": CONF})
    assert gone.status_code in (404, 405)
    src = client.get("/api/branding/source").json()
    assert src["read_only"] is True and "hubzoid.yaml" in src["how_to_change"]
    publish(client)
    assert client.get("/api/branding").json()["name"] == "Example Operations"
    # A broken edit keeps the last good brand (and its terms), and says so.
    (hub / "hubzoid.yaml").write_text("version: 1\nhubs: ['.']\nbrand:\n  identity: {nam: x}\n")
    assert client.get("/api/branding").json()["name"] == "Example Operations"
    state = client.get("/api/branding/source").json()["active"]
    assert state["valid"] is False and "nam" in state["error"]


def test_validate_checks_the_folder(hub):
    from hubzoid.cli import app as cli_app

    publish(None, files={"branding/logo.svg": SVG})  # the favicon is missing
    out = CliRunner().invoke(cli_app, ["branding", "validate", str(hub), "--json"])
    assert out.exit_code != 0 and "favicon.png" in out.output
    publish(None)
    ok = CliRunner().invoke(cli_app, ["branding", "validate", str(hub), "--json"])
    assert ok.exit_code == 0 and json.loads(ok.output)["source"] == "hubzoid.yaml (brand)"
