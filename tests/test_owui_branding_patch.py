"""Static OWUI branding patch: tab title, link-share meta, and PWA manifest.

WEBUI_NAME only renames surfaces the SPA renders at runtime. The browser
tab title before hydration, the meta a link-preview crawler reads, and the
site.webmanifest name are served as static files — they ship as
"Open WebUI". `webui._patch_owui_branding` rewrites them. These tests are
hermetic: we build a fake OWUI static tree and point `branding.static_dirs`
at it, so nothing touches the installed package.
"""
from __future__ import annotations

import json

import pytest

from hubzoid import webui


_OWUI_INDEX = """\
<!doctype html>
<html lang="en">
\t<head>
\t\t<meta charset="utf-8" />
\t\t<title>Open WebUI</title>
\t\t<link href="/app.js" rel="modulepreload">
\t</head>
\t<body></body>
</html>
"""

_OWUI_MANIFEST = json.dumps(
    {"name": "Open WebUI", "short_name": "WebUI", "display": "standalone"}
)


@pytest.fixture
def fake_owui(tmp_path, monkeypatch):
    """A fake OWUI static tree wired into branding.static_dirs()."""
    frontend = tmp_path / "frontend"
    (frontend / "static").mkdir(parents=True)
    (frontend / "index.html").write_text(_OWUI_INDEX)
    (frontend / "static" / "site.webmanifest").write_text(_OWUI_MANIFEST)
    monkeypatch.setattr(webui.branding, "static_dirs", lambda: [frontend])
    return frontend


def test_title_rebranded(fake_owui):
    webui._patch_owui_branding("Hubzoid", strip=True)
    assert "<title>Hubzoid</title>" in (fake_owui / "index.html").read_text()
    assert "Open WebUI" not in (fake_owui / "index.html").read_text()


def test_link_share_meta_injected(fake_owui):
    webui._patch_owui_branding("Hubzoid", strip=True)
    html = (fake_owui / "index.html").read_text()
    assert '<meta property="og:title" content="Hubzoid" />' in html
    assert '<meta property="og:description" content="Hubzoid" />' in html
    assert '<meta name="description" content="Hubzoid" />' in html


def test_manifest_rebranded(fake_owui):
    webui._patch_owui_branding("Hubzoid", strip=True)
    data = json.loads((fake_owui / "static" / "site.webmanifest").read_text())
    assert data["name"] == "Hubzoid"
    assert data["short_name"] == "Hubzoid"
    assert data["display"] == "standalone"  # untouched keys preserved


def test_idempotent_no_duplicate_block(fake_owui):
    webui._patch_owui_branding("Hubzoid", strip=True)
    webui._patch_owui_branding("Hubzoid", strip=True)
    html = (fake_owui / "index.html").read_text()
    assert html.count("hubzoid-branding:start") == 1


def test_rebrand_to_new_name_updates_title(fake_owui):
    webui._patch_owui_branding("Hubzoid", strip=True)
    webui._patch_owui_branding("ACME Support", strip=True)
    html = (fake_owui / "index.html").read_text()
    assert "<title>ACME Support</title>" in html
    assert html.count("hubzoid-branding:start") == 1
    assert '<meta property="og:title" content="ACME Support" />' in html


def test_strip_false_restores_owui_default(fake_owui):
    webui._patch_owui_branding("Hubzoid", strip=True)
    webui._patch_owui_branding("Hubzoid", strip=False)
    html = (fake_owui / "index.html").read_text()
    assert "<title>Open WebUI</title>" in html
    assert "hubzoid-branding" not in html
    data = json.loads((fake_owui / "static" / "site.webmanifest").read_text())
    assert data["name"] == "Open WebUI"
    assert data["short_name"] == "WebUI"


def test_html_escaping_in_brand(fake_owui):
    webui._patch_owui_branding('A & B "Co"', strip=True)
    html = (fake_owui / "index.html").read_text()
    assert "<title>A &amp; B &quot;Co&quot;</title>" in html


def test_default_brand_is_hubzoid_when_unnamed(captured_env_owui):
    """webui.start passes 'Hubzoid' to the patch when webui_name is None."""
    brands = captured_env_owui
    assert brands and brands[-1] == "Hubzoid"


@pytest.fixture
def captured_env_owui(tmp_path, monkeypatch):
    """Spy on the brand handed to _patch_owui_branding during a start()."""
    monkeypatch.setattr(webui, "_find_binary", lambda: "/fake/open-webui")
    monkeypatch.setattr(webui, "_patch_owui_suffix", lambda strip: None)
    brands: list[str] = []
    monkeypatch.setattr(
        webui, "_patch_owui_branding", lambda brand, *, strip: brands.append(brand)
    )

    def fake_popen(cmd, env=None, stdout=None, stderr=None):
        from unittest.mock import MagicMock

        proc = MagicMock()
        proc._log_path = tmp_path / "log"
        return proc

    monkeypatch.setattr(webui.subprocess, "Popen", fake_popen)
    hub = tmp_path / "hub"
    hub.mkdir()
    webui.start(
        hub_dir=hub,
        bridge_port=8000,
        ui_port=3080,
        api_key="dev",
        model_label="hub",
        webui_name=None,
        suggestions=[],
    )
    return brands
