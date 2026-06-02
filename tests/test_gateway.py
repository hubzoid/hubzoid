"""Tests for `hubzoid gateway` (#3b: one Open WebUI fronting many bridges).

The planning logic (connection env, edge routes, key/url alignment, slug
dedup, port-collision guard) is pure and unit-tested with a fake settings
loader. A CLI wiring test confirms the command threads the plan into the
shared OWUI's connection env and the edge's per-hub artifact routes.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from hubzoid import cli, gateway
from hubzoid.settings import Settings


def _settings(hub_dir: Path, bridge_port: int, keys=("k",), label=None) -> Settings:
    return Settings(
        hub_dir=Path(hub_dir).resolve(),
        model="claude-local",
        model_label=label,
        bridge_api_keys=tuple(keys),
        webui_name=None,
        ui_port=3080,
        bridge_port=bridge_port,
        log_level="info",
        max_upload_bytes=1,
    )


def _loader(mapping: dict[str, Settings]):
    def load(hub_dir):
        return mapping[str(Path(hub_dir).resolve())]
    return load


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def test_plan_builds_aligned_connection_env(tmp_path):
    irs, gpms = tmp_path / "irs", tmp_path / "gpms"
    irs.mkdir(); gpms.mkdir()
    load = _loader({
        str(irs): _settings(irs, 8000, keys=("irs-key",), label="irs-agent"),
        str(gpms): _settings(gpms, 8001, keys=("gpms-key",), label="gpms-agent"),
    })
    gp = gateway.plan([irs, gpms], load=load)
    env = gp.connection_env()
    assert env["ENABLE_OPENAI_API"] == "True"
    assert env["OPENAI_API_BASE_URLS"] == "http://127.0.0.1:8000/v1;http://127.0.0.1:8001/v1"
    # Keys are positionally aligned with the URLs.
    assert env["OPENAI_API_KEYS"] == "irs-key;gpms-key"


def test_plan_edge_routes_namespace_per_hub(tmp_path):
    irs, gpms = tmp_path / "irs", tmp_path / "gpms"
    irs.mkdir(); gpms.mkdir()
    load = _loader({
        str(irs): _settings(irs, 8000),
        str(gpms): _settings(gpms, 8001),
    })
    gp = gateway.plan([irs, gpms], load=load)
    routes = gp.edge_routes()
    assert routes == [
        {"prefix": "/b/irs/artifacts", "upstream": "http://127.0.0.1:8000", "strip_prefix": "/b/irs"},
        {"prefix": "/b/gpms/artifacts", "upstream": "http://127.0.0.1:8001", "strip_prefix": "/b/gpms"},
    ]


def test_plan_public_url_per_hub(tmp_path):
    irs = tmp_path / "irs"; irs.mkdir()
    gp = gateway.plan([irs], load=_loader({str(irs): _settings(irs, 8000)}))
    assert gp.public_url_for("https://hub.example.com/", gp.backends[0]) == "https://hub.example.com/b/irs"


def test_plan_dedupes_colliding_slugs(tmp_path):
    a = tmp_path / "a" / "hub"; a.mkdir(parents=True)
    b = tmp_path / "b" / "hub"; b.mkdir(parents=True)
    load = _loader({str(a): _settings(a, 8000), str(b): _settings(b, 8001)})
    gp = gateway.plan([a, b], load=load)
    assert [x.slug for x in gp.backends] == ["hub", "hub-2"]


def test_plan_rejects_duplicate_bridge_ports(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    load = _loader({str(a): _settings(a, 8000), str(b): _settings(b, 8000)})
    with pytest.raises(ValueError, match="BRIDGE_PORT"):
        gateway.plan([a, b], load=load)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------
def test_gateway_command_wires_owui_and_edge(tmp_path, monkeypatch):
    irs, gpms = tmp_path / "irs", tmp_path / "gpms"
    for h in (irs, gpms):
        h.mkdir()
        (h / "AGENTS.md").write_text("---\nname: x\n---\nbody")

    fake_plan = gateway.GatewayPlan(backends=(
        gateway.GatewayBackend(hub_dir=irs, slug="irs", bridge_port=8000, api_key="irs-key", model_label="irs-agent"),
        gateway.GatewayBackend(hub_dir=gpms, slug="gpms", bridge_port=8001, api_key="gpms-key", model_label="gpms-agent"),
    ))
    monkeypatch.setattr(gateway, "plan", lambda hub_dirs: fake_plan)

    captured = {}

    def fake_start_gateway(**kwargs):
        captured["connection_env"] = kwargs["connection_env"]
        proc = MagicMock()
        proc._log_path = tmp_path / "log"
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        return proc
    from hubzoid import webui
    monkeypatch.setattr(webui, "start_gateway", fake_start_gateway)

    popen_calls = []

    def fake_popen(cmd, env=None, **kw):
        popen_calls.append({"cmd": cmd, "env": env or {}})
        proc = MagicMock()
        proc.poll.return_value = None
        return proc
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)

    result = CliRunner().invoke(
        cli.app,
        ["gateway", str(irs), str(gpms), "--public-url", "https://host", "--port", "3080"],
    )
    assert result.exit_code == 0, result.output

    # Shared OWUI got both bridges, positionally aligned.
    conn = captured["connection_env"]
    assert conn["OPENAI_API_BASE_URLS"] == "http://127.0.0.1:8000/v1;http://127.0.0.1:8001/v1"
    assert conn["OPENAI_API_KEYS"] == "irs-key;gpms-key"

    # Two bridges launched headless on their ports.
    bridge_cmds = [c["cmd"] for c in popen_calls if "run" in c["cmd"]]
    assert len(bridge_cmds) == 2
    for c in bridge_cmds:
        assert "--no-ui" in c and "--bridge-port" in c

    # Per-hub public URL injected into each bridge's env.
    bridge_envs = [c["env"] for c in popen_calls if "run" in c["cmd"]]
    pub_urls = sorted(e.get("HUBZOID_PUBLIC_URL", "") for e in bridge_envs)
    assert pub_urls == ["https://host/b/gpms", "https://host/b/irs"]

    # The edge got per-hub artifact routes.
    edge_env = next(c["env"] for c in popen_calls if "hubzoid.edge:_factory" in c["cmd"])
    assert "/b/irs/artifacts" in edge_env["HUBZOID_EDGE_ROUTES"]
    assert "/b/gpms/artifacts" in edge_env["HUBZOID_EDGE_ROUTES"]
