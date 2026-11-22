"""The brand's files as the deployment folder holds them
(hubzoid.ui.branding_store): a decorative stylesheet is re-rendered from its
safe rules, never served as written; a stylesheet that isn't UTF-8, brand
files over the total limit, a broken theme.json or a file that is a symlink
out of the folder never break the app (the brand falls back and says why in
the log); only the files the brand supplied are exported, never the
generated icons. Uses the deployment of tests/test_branding_folder.py."""
from __future__ import annotations

import logging

import pytest

from hubzoid.ui import branding_assets, branding_store
from tests.test_branding_folder import CONF, FILES, SVG, hub, png, publish  # noqa: F401

CSS = b'[data-brand-slot="brand.welcome"]{background-color:#FFF5DA;padding:8px 12px}'
WITH_CSS = {**CONF, "presentation": {"decorative_css": "branding/brand.css"}}


def service(hub):  # noqa: F811
    return branding_store.service_for(hub)


def assets_by_key(src):
    return {a["key"]: a for a in src["assets"]}


def test_a_decorative_stylesheet_is_re_rendered_from_its_safe_rules(hub):  # noqa: F811
    publish(None, conf=WITH_CSS, files={**FILES, "branding/brand.css": CSS})
    svc = service(hub)
    css = assets_by_key(svc.active_source())["decorative_css"]
    data, media = svc.asset_bytes(css["digest"])
    assert media == "text/css" and b"background-color" in data and data != CSS     # rendered, not copied
    assert svc.asset_bytes("sha256:" + "0" * 64) is None


def test_only_supplied_files_are_exported(hub):  # noqa: F811
    publish(None)
    svc = service(hub)
    generated = {a["key"] for a in svc.active_source()["assets"] if a.get("generated")}
    assert generated == set(branding_assets.ICON_SIZES)                 # from the raster favicon
    files = svc.active_files()
    assert set(files) == set(FILES) and files["branding/logo.svg"] == SVG
    assert files["branding/favicon.png"].startswith(b"\x89PNG")       # re-encoded, still the favicon


@pytest.mark.parametrize("files,limit,why", [
    ({**FILES, "branding/brand.css": b"\xff\xfe not text"}, None, "not UTF-8"),
    ({**FILES, "branding/brand.css": CSS}, 100, "more than 20 MiB"),
])
def test_bad_brand_files_fall_back_and_say_why(hub, monkeypatch, caplog, files, limit, why):  # noqa: F811
    if limit:
        monkeypatch.setattr(branding_assets, "MAX_TOTAL_BYTES", limit)
    publish(None, conf=WITH_CSS, files=files)
    with caplog.at_level(logging.WARNING, logger="hubzoid.ui.branding_store"):
        src = service(hub).active_source()
    assert src["assets"] == [] and src["configuration"]["identity"]["name"] == "Example Operations"
    assert any(why in r.getMessage() for r in caplog.records)


def test_a_broken_theme_or_an_escaping_file_never_breaks_the_brand(hub, tmp_path, caplog):  # noqa: F811
    publish(None, theme={"x": 1})
    (hub / "branding" / "theme.json").write_text("{not json")
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    (hub / "branding" / "favicon.png").unlink()
    (hub / "branding" / "favicon.png").symlink_to(outside)
    with caplog.at_level(logging.WARNING, logger="hubzoid.ui.branding_store"):
        src = service(hub).active_source()
    assert src["theme"] is None and "favicon" not in assets_by_key(src)
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "theme.json is not usable" in messages and "names files that aren't there: branding/favicon.png" in messages
