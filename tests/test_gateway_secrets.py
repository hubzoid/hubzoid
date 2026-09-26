"""Secrets across processes: one hub's secrets never reach another hub, the
gateway process, the shared Open WebUI or the edge. The deployment secret
reaches Open WebUI, and bridges get only BRIDGE_DEPLOYMENT_KEYS from it."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from hubzoid import branding, cli, config_secrets as cs, deployment, settings, webui
from tests._fake_secrets import clean_process_env, install

DEPLOYMENT_SECRET = {
    "WEBUI_SECRET_KEY": "deployment-signing-key",
    "WEBUI_AUTH": "true",
    "GOOGLE_CLIENT_SECRET": "google-client-secret-value",
    "HUBZOID_GATEWAY_ADMIN_PASSWORD": "admin-password-value",
    "HUBZOID_PUBLIC_URL": "https://hub.example.com",
}
HUB_VALUES = ("alpha-hub-file-value", "alpha-hub-secret-value", "alpha-tool-token",
              "alpha-restricted-secret-value", "beta-hub-file-value", "beta-hub-secret-value",
              "beta-tool-token", "beta-restricted-secret-value")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Never stamp branding into, or patch, the installed Open WebUI.
    monkeypatch.setattr(branding, "static_dirs", lambda: [])
    monkeypatch.setattr(webui, "_patch_owui_suffix", lambda strip: None)
    monkeypatch.setattr(webui, "_patch_owui_branding", lambda brand, strip: None)
    monkeypatch.setattr(webui, "_find_binary", lambda: "/fake/open-webui")
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)
    with clean_process_env():
        yield


@pytest.fixture
def launched(monkeypatch):
    calls: list[dict] = []

    def fake_popen(cmd, *a, env=None, **kw):
        calls.append({"cmd": [str(c) for c in cmd], "env": dict(env or {})})
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


def _hubs(tmp_path: Path) -> tuple[Path, Path]:
    out = []
    for i, name in enumerate(("alpha", "beta")):
        hub = tmp_path / name
        (hub / "restricted").mkdir(parents=True)
        (hub / "AGENTS.md").write_text(f"---\nname: {name}\n---\nbody")
        (hub / ".env").write_text(f"BRIDGE_PORT={8100 + i}\nBRIDGE_API_KEYS={name}-bridge-key-long-enough\n"
                                  f"{name.upper()}_ONLY={name}-hub-file-value\n"
                                  f"HUBZOID_HUB_SECRET_NAME={name}-hub\n")
        (hub / "restricted" / ".env").write_text(f"{name.upper()}_TOOL_TOKEN={name}-tool-token\n"
                                                 f"HUBZOID_RESTRICTED_SECRET_NAME={name}-restricted\n")
        out.append(hub)
    return out[0], out[1]


def _secrets():
    return {
        "dep": DEPLOYMENT_SECRET,
        "alpha-hub": {"ALPHA_HUB_SECRET": "alpha-hub-secret-value"},
        "alpha-restricted": {"ALPHA_RESTRICTED_SECRET": "alpha-restricted-secret-value"},
        "beta-hub": {"BETA_HUB_SECRET": "beta-hub-secret-value"},
        "beta-restricted": {"BETA_RESTRICTED_SECRET": "beta-restricted-secret-value"},
    }


def _by_kind(calls):
    bridges = {c["cmd"][4]: c["env"] for c in calls if c["cmd"][2:4] == ["hubzoid", "run"]}
    owui = next(c["env"] for c in calls if c["cmd"][0] == "/fake/open-webui")
    edge = next(c["env"] for c in calls if "hubzoid.edge:_factory" in c["cmd"])
    return bridges, owui, edge


def _no_hub_values(env: dict) -> None:
    leaked = [v for v in HUB_VALUES if v in env.values()]
    assert not leaked, leaked
    assert not any(k.startswith(("ALPHA_", "BETA_")) for k in env)


def _gateway(tmp_path, alpha, beta, *extra):
    return CliRunner().invoke(cli.app, ["gateway", str(alpha), str(beta), "--data-dir",
                                        str(tmp_path / "gw"), "--port", "3080", *extra])


def test_gateway_scopes_the_deployment_secret_and_never_reads_hub_secrets(tmp_path, monkeypatch, launched):
    alpha, beta = _hubs(tmp_path)
    os.environ.update({"AWS_SECRET_NAME": "dep", "AWS_REGION": "eu-west-1",
                       "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE", "AWS_SECRET_ACCESS_KEY": "aws-secret-value"})
    fake = install(monkeypatch, _secrets())

    result = _gateway(tmp_path, alpha, beta)
    assert result.exit_code == 0, result.output

    # The gateway read the deployment secret only.
    assert fake.calls == [("dep", "eu-west-1")]
    bridges, owui, edge = _by_kind(launched)

    # The shared Open WebUI gets the whole deployment secret, minus Hubzoid
    # keys, secret names and the AWS credentials used to read it.
    assert owui["WEBUI_SECRET_KEY"] == "deployment-signing-key"
    assert owui["GOOGLE_CLIENT_SECRET"] == "google-client-secret-value"
    assert not any(k.startswith("HUBZOID_") for k in owui)
    assert "AWS_SECRET_NAME" not in owui and "AWS_ACCESS_KEY_ID" not in owui
    assert "AWS_SECRET_ACCESS_KEY" not in owui
    _no_hub_values(owui)
    _no_hub_values(edge)

    # Bridges get only the bridge keys, per-hub URLs, and do not re-fetch.
    assert set(bridges) == {str(alpha), str(beta)}
    for hub, env in bridges.items():
        assert env["WEBUI_SECRET_KEY"] == "deployment-signing-key"
        assert env["HUBZOID_GATEWAY_ADMIN_PASSWORD"] == "admin-password-value"
        assert "GOOGLE_CLIENT_SECRET" not in env
        assert env[cs.INHERITED_MARKER] == "1"
        assert env["HUBZOID_PUBLIC_URL"] == f"https://hub.example.com/b/{Path(hub).name}"
        _no_hub_values(env)

    # The ignored keys are named. No value is printed.
    assert "GOOGLE_CLIENT_SECRET" in result.output
    for value in (*DEPLOYMENT_SECRET.values(), *HUB_VALUES, "aws-secret-value"):
        if value not in ("true",):
            assert value not in result.output

    # The manifest names the secret for external bridges, and holds no value.
    manifest = (tmp_path / "gw" / "deployment.json").read_text()
    assert json.loads(manifest)["deployment_secret"] == {"name": "dep", "region": "eu-west-1"}
    # Credentials never land in the manifest. The public address is not one: it
    # is recorded so bridges and CLI jobs build report links for the right site.
    assert not any(v in manifest for k, v in DEPLOYMENT_SECRET.items()
                   if v != "true" and k != "HUBZOID_PUBLIC_URL")
    assert json.loads(manifest)["public_url"] == "https://hub.example.com"
    assert json.loads(manifest)["hubs"][0]["slug"] == "alpha"

    # The gateway process itself never held a hub's secrets.
    _no_hub_values(dict(os.environ))

    # Each bridge then loads its own hub: beta sees beta's secrets and nothing of alpha's.
    beta_env = bridges[str(beta)]
    os.environ.clear()
    os.environ.update(beta_env)
    cs.clear_cache()
    fake.calls.clear()
    settings.load(beta)
    assert sorted(fake.names()) == ["beta-hub", "beta-restricted"]
    assert os.environ["BETA_HUB_SECRET"] == "beta-hub-secret-value"
    assert os.environ["BETA_RESTRICTED_SECRET"] == "beta-restricted-secret-value"
    assert not any(k.startswith("ALPHA_") for k in os.environ)
    assert os.environ["HUBZOID_PUBLIC_URL"] == "https://hub.example.com/b/beta"


def test_external_bridge_fetches_the_deployment_secret_from_the_manifest(tmp_path, monkeypatch, launched):
    alpha, beta = _hubs(tmp_path)
    os.environ.update({"AWS_SECRET_NAME": "dep", "AWS_REGION": "eu-west-1"})
    fake = install(monkeypatch, _secrets())
    result = _gateway(tmp_path, alpha, beta, "--no-bridges")
    assert result.exit_code == 0, result.output
    assert not [c for c in launched if c["cmd"][2:4] == ["hubzoid", "run"]]

    # A bridge unit that sets only HOME and PATH, as on the live deployment.
    os.environ.clear()
    os.environ.update({k: v for k, v in _by_kind(launched)[2].items() if k in ("PATH", "HOME")})
    cs.clear_cache()
    fake.calls.clear()
    settings.load(alpha)
    assert sorted(fake.names()) == ["alpha-hub", "alpha-restricted", "dep"]
    assert os.environ["WEBUI_SECRET_KEY"] == "deployment-signing-key"
    assert "GOOGLE_CLIENT_SECRET" not in os.environ
    assert os.environ["HUBZOID_PUBLIC_URL"] == "https://hub.example.com/b/alpha"
    assert not any(k.startswith("BETA_") for k in os.environ)


def test_hub_env_cannot_name_the_deployment_secret(tmp_path, monkeypatch, launched):
    alpha, beta = _hubs(tmp_path)
    with (alpha / ".env").open("a") as f:
        f.write("AWS_SECRET_NAME=alpha-redirect\n")
    fake = install(monkeypatch, {"alpha-redirect": {"DATABASE_URL": "postgresql://elsewhere"}})
    result = _gateway(tmp_path, alpha, beta)
    assert result.exit_code == 0, result.output
    assert fake.calls == []
    assert "AWS_SECRET_NAME in alpha/.env is ignored" in result.output
    assert "deployment_secret" not in json.loads((tmp_path / "gw" / "deployment.json").read_text())


def test_unreadable_deployment_secret_stops_the_gateway(tmp_path, monkeypatch, launched):
    alpha, beta = _hubs(tmp_path)
    os.environ.update({"AWS_SECRET_NAME": "dep", "AWS_REGION": "eu-west-1"})
    install(monkeypatch, {})
    result = _gateway(tmp_path, alpha, beta)
    assert result.exit_code == 1
    assert "deployment secret 'dep'" in result.output and "ResourceNotFoundException" in result.output
    assert launched == []


def test_sign_in_keys_from_hub_env_still_reach_open_webui_without_secrets(tmp_path, monkeypatch, launched):
    """0.9.x compatibility, unchanged: with nothing named, WEBUI_* keys kept in a
    hub .env reach the shared Open WebUI. Other hub keys do not."""
    alpha, beta = _hubs(tmp_path)
    for hub in (alpha, beta):
        text = (hub / ".env").read_text().replace(f"HUBZOID_HUB_SECRET_NAME={hub.name}-hub\n", "")
        (hub / ".env").write_text(text)
        (hub / "restricted" / ".env").write_text(f"{hub.name.upper()}_TOOL_TOKEN={hub.name}-tool-token\n")
    with (alpha / ".env").open("a") as f:
        f.write("WEBUI_SECRET_KEY=hub-kept-key\nWEBUI_AUTH=true\n")
    monkeypatch.setattr(cs, "_client", lambda region: pytest.fail("nothing is named"))
    result = _gateway(tmp_path, alpha, beta)
    assert result.exit_code == 0, result.output
    bridges, owui, edge = _by_kind(launched)
    assert owui["WEBUI_SECRET_KEY"] == "hub-kept-key"
    _no_hub_values(owui)
    _no_hub_values(edge)
    for env in bridges.values():
        assert env["WEBUI_SECRET_KEY"] == "hub-kept-key"  # as before
        assert cs.INHERITED_MARKER not in env
        _no_hub_values(env)
    assert "deployment_secret" not in json.loads((tmp_path / "gw" / "deployment.json").read_text())


def test_deployment_secret_wins_over_hub_env_compatibility_keys(tmp_path, monkeypatch, launched):
    alpha, beta = _hubs(tmp_path)
    with (alpha / ".env").open("a") as f:
        f.write("WEBUI_SECRET_KEY=hub-kept-key\n")
    os.environ.update({"AWS_SECRET_NAME": "dep", "AWS_REGION": "eu-west-1"})
    install(monkeypatch, _secrets())
    result = _gateway(tmp_path, alpha, beta)
    assert result.exit_code == 0, result.output
    assert _by_kind(launched)[1]["WEBUI_SECRET_KEY"] == "deployment-signing-key"


def test_standalone_run_keeps_hub_only_layers_out_of_open_webui_and_edge(tmp_path, monkeypatch, launched):
    hub = tmp_path / "solo"
    (hub / "restricted").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: solo\n---\nbody")
    (hub / ".env").write_text("BRIDGE_API_KEYS=solo-bridge-key-long-enough\nAWS_SECRET_NAME=dep\n"
                              "AWS_REGION=eu-west-1\nHUBZOID_HUB_SECRET_NAME=solo-hub\nSHARED=hub\n")
    (hub / "restricted" / ".env").write_text("SOLO_TOOL_TOKEN=solo-tool-token\nSHARED=restricted\n"
                                             "HUBZOID_RESTRICTED_SECRET_NAME=solo-restricted\n")
    install(monkeypatch, {"dep": {"WEBUI_SECRET_KEY": "deployment-signing-key", "WEBUI_AUTH": "true"},
                          "solo-hub": {"SOLO_HUB_SECRET": "solo-hub-secret-value"},
                          "solo-restricted": {"SOLO_RESTRICTED_SECRET": "solo-restricted-secret-value"}})
    cli.run(hub=hub, port=3090, bridge_port=8190, host="127.0.0.1", no_ui=False,
            slack=False, whatsapp=False, telegram=False, webhook=False)

    bridge = next(c["env"] for c in launched if "hubzoid.server:build_app" in c["cmd"])
    owui = next(c["env"] for c in launched if c["cmd"][0] == "/fake/open-webui")
    edge = next(c["env"] for c in launched if "hubzoid.edge:_factory" in c["cmd"])
    for env in (bridge, owui, edge):
        assert "SOLO_TOOL_TOKEN" not in env and "SOLO_RESTRICTED_SECRET" not in env
        assert "SOLO_HUB_SECRET" not in env
        assert env["SHARED"] == "hub"
    assert owui["WEBUI_SECRET_KEY"] == "deployment-signing-key"
    assert not any(k.startswith("HUBZOID_") for k in owui) and "AWS_SECRET_NAME" not in owui
    assert bridge["HUBZOID_HUB_DIR"] == str(hub.resolve())
    # The run process itself holds the full hub view: the bridge reloads it.
    assert os.environ["SOLO_TOOL_TOKEN"] == "solo-tool-token"
    assert os.environ["SOLO_HUB_SECRET"] == "solo-hub-secret-value"


def test_standalone_run_stops_on_an_unreadable_secret(tmp_path, monkeypatch, launched):
    hub = tmp_path / "solo"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: solo\n---\nbody")
    (hub / ".env").write_text("HUBZOID_HUB_SECRET_NAME=solo-hub\nAWS_REGION=eu-west-1\n")
    install(monkeypatch, {})
    result = CliRunner().invoke(cli.app, ["run", str(hub), "--no-ui"])
    assert result.exit_code == 1
    assert "hub secret 'solo-hub'" in result.output
    assert launched == []


def test_deployment_manifest_round_trips_the_pointer(tmp_path):
    hub = tmp_path / "h"
    hub.mkdir()
    deployment.save(tmp_path / "m.json", hubs=[dict(key="h", path=str(hub))], operational_url="sqlite://",
                    owui_url="http://127.0.0.1:1", owui_db="x", deployment_secret={"name": "dep", "region": None})
    assert deployment.deployment_secret(hub) == {"name": "dep", "region": None}
    deployment.save(tmp_path / "m.json", hubs=[dict(key="h", path=str(hub))], operational_url="sqlite://",
                    owui_url="http://127.0.0.1:1", owui_db="x")
    assert deployment.deployment_secret(hub) is None
    assert "deployment_secret" not in (tmp_path / "m.json").read_text()
