"""The brand's name where the server writes text itself: the page title before
the app loads, the assistant-connection pages, and "Hubzoid" without a brand."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hubzoid import mcp_oauth_web, webapp
from hubzoid.ui import branding_models
from hubzoid.ui.branding_store import brand_name
from tests.chat_helpers import build_app, make_hub
from tests.test_branding_folder import publish


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


def test_brand_name_follows_the_brand(hub):
    assert brand_name(hub) == "Hubzoid"
    publish()
    assert brand_name(hub) == "Example Operations"


@pytest.mark.skipif(not (webapp.DIST / "index.html").is_file(), reason="web app bundle not built")
def test_the_tab_shows_the_brand_before_the_app_loads(hub):
    client = TestClient(build_app())
    assert "<title>Hubzoid</title>" in client.get("/").text
    publish()
    page = client.get("/").text
    assert "<title>Example Operations</title>" in page and "<title>Hubzoid</title>" not in page


def test_assistant_connection_pages_carry_the_brand():
    plain = mcp_oauth_web._notice("Connection expired", "Start again.").body.decode()
    assert "<b>/</b>hubzoid" in plain and "Back to Hubzoid" in plain and "Connection expired · Hubzoid" in plain
    branded = mcp_oauth_web._notice("Connection expired", "Start again.", brand="Acme & Co").body.decode()
    assert "Back to Acme &amp; Co" in branded and "Connection expired · Acme &amp; Co" in branded
    assert "hubzoid" not in branded.lower()
    kept = mcp_oauth_web._notice("Couldn't confirm", "Reload.", action="Reload connections", brand="Acme").body.decode()
    assert "Reload connections" in kept and "Back to" not in kept


def test_a_brand_can_hide_the_sign_in_mark():
    assert "hubzoid_mark" in branding_models.HIDEABLE
