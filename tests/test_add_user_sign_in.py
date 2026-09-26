"""How the Console learns which sign-in modes Add user may offer.

Google sign-in only is offered when the chat app attaches a Google sign-in to
an existing account by email (Google configured and
OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true). The gateway records those facts, never
their values, in the deployment manifest so separately started bridges see
them; a standalone hub reads its own environment. No model, no network.
"""
from __future__ import annotations

import json
import os

import pytest

from hubzoid import deployment
from hubzoid.access import accounts as accountlib

from tests.test_gateway_secrets import _clean, _gateway, _hubs, launched  # noqa: F401 — fixtures

GOOGLE = {"GOOGLE_CLIENT_ID": "1234-google-client-id.apps.example",
          "GOOGLE_CLIENT_SECRET": "google-client-secret-value"}


@pytest.mark.parametrize("env,flags", [
    ({}, {"google": False, "merge_by_email": False}),
    ({**GOOGLE}, {"google": True, "merge_by_email": False}),
    ({"GOOGLE_CLIENT_ID": "id"}, {"google": False, "merge_by_email": False}),
    ({**GOOGLE, "OAUTH_MERGE_ACCOUNTS_BY_EMAIL": "True"}, {"google": True, "merge_by_email": True}),
    # Open WebUI compares the lower-cased value to "true" without stripping.
    ({"OAUTH_MERGE_ACCOUNTS_BY_EMAIL": "yes"}, {"google": False, "merge_by_email": False}),
    ({"OAUTH_ALLOWED_DOMAINS": "*"}, {"google": False, "merge_by_email": False}),
    ({"OAUTH_ALLOWED_DOMAINS": "x.org, y.org"},
     {"google": False, "merge_by_email": False, "allowed_domains": ["x.org", "y.org"]}),
    ({"ENABLE_OAUTH_PERSISTENT_CONFIG": "true"},
     {"google": False, "merge_by_email": False, "oauth_settings_in_app": True}),
])
def test_sign_in_flags_parse_like_open_webui(env, flags):
    assert deployment.sign_in_flags(env) == flags


def test_manifest_records_sign_in_flags_without_values(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    env = {**GOOGLE, "OAUTH_MERGE_ACCOUNTS_BY_EMAIL": "true", "OAUTH_ALLOWED_DOMAINS": "x.org"}
    deployment.save(tmp_path / "gw" / "deployment.json",
                    hubs=[dict(key="hub", name="Hub", path=str(hub), model_id="hub")],
                    operational_url="sqlite://", owui_url="http://127.0.0.1:1", owui_db="x",
                    sign_in=deployment.sign_in_flags(env))
    text = (tmp_path / "gw" / "deployment.json").read_text()
    assert json.loads(text)["sign_in"] == {"google": True, "merge_by_email": True,
                                           "allowed_domains": ["x.org"]}
    for value in GOOGLE.values():
        assert value not in text
    # Omitted entirely when not given (an older manifest has none).
    deployment.save(tmp_path / "gw2" / "deployment.json",
                    hubs=[dict(key="hub", name="Hub", path=str(hub), model_id="hub")],
                    operational_url="sqlite://", owui_url="http://127.0.0.1:1", owui_db="x")
    assert "sign_in" not in json.loads((tmp_path / "gw2" / "deployment.json").read_text())


def test_gateway_records_flags_for_bridges_started_separately(tmp_path, launched):  # noqa: F811
    alpha, beta = _hubs(tmp_path)
    os.environ.update({**GOOGLE, "OAUTH_MERGE_ACCOUNTS_BY_EMAIL": "true"})
    result = _gateway(tmp_path, alpha, beta, "--no-bridges")
    assert result.exit_code == 0, result.output
    text = (tmp_path / "gw" / "deployment.json").read_text()
    assert json.loads(text)["sign_in"] == {"google": True, "merge_by_email": True}
    assert not any(v in text for v in GOOGLE.values())
    # A bridge unit with none of the gateway's environment still sees them.
    os.environ.clear()
    assert accountlib.sign_in_options(alpha) == {"password": True, "google": True}


def test_gateway_without_merge_offers_no_google(tmp_path, launched):  # noqa: F811
    alpha, beta = _hubs(tmp_path)
    os.environ.update(GOOGLE)
    assert _gateway(tmp_path, alpha, beta).exit_code == 0
    assert json.loads((tmp_path / "gw" / "deployment.json").read_text())["sign_in"] == {
        "google": True, "merge_by_email": False}
    assert accountlib.sign_in_options(alpha)["google"] is False


def test_standalone_hub_reads_its_own_environment(tmp_path, monkeypatch):
    for key in ("HUBZOID_DEPLOYMENT", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                "OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "OAUTH_ALLOWED_DOMAINS",
                "ENABLE_OAUTH_PERSISTENT_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    hub = tmp_path / "solo"
    hub.mkdir()
    assert accountlib.sign_in_options(hub) == {"password": True, "google": False}
    (hub / ".env").write_text("GOOGLE_CLIENT_ID=id\nGOOGLE_CLIENT_SECRET=secret\n")
    assert accountlib.sign_in_options(hub)["google"] is False
    monkeypatch.setenv("OAUTH_MERGE_ACCOUNTS_BY_EMAIL", "true")
    assert accountlib.sign_in_options(hub) == {"password": True, "google": True}
    monkeypatch.setenv("OAUTH_ALLOWED_DOMAINS", "x.org")
    assert accountlib.sign_in_options(hub)["google_domains"] == ["x.org"]
    # OAuth settings kept inside the chat app can't be read from here: not offered.
    monkeypatch.setenv("ENABLE_OAUTH_PERSISTENT_CONFIG", "true")
    assert accountlib.sign_in_options(hub)["google"] is False
