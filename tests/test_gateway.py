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
    """Same folder name in different parents: artifact slugs dedup (hub,
    hub-2). The agents carry distinct names so their model ids differ —
    identical model ids are rejected outright (see the collision test)."""
    a = tmp_path / "a" / "hub"; a.mkdir(parents=True)
    b = tmp_path / "b" / "hub"; b.mkdir(parents=True)
    (a / "AGENTS.md").write_text("---\nname: Agent A\n---\nbody")
    (b / "AGENTS.md").write_text("---\nname: Agent B\n---\nbody")
    load = _loader({str(a): _settings(a, 8000), str(b): _settings(b, 8001)})
    gp = gateway.plan([a, b], load=load)
    assert [x.slug for x in gp.backends] == ["hub", "hub-2"]


def test_plan_captures_per_hub_identity(tmp_path):
    """The plan carries each hub's display identity (name, description,
    suggestions from AGENTS.md frontmatter, logo from branding/) so the
    gateway can provision per-model config in Open WebUI."""
    irs = tmp_path / "irs"
    irs.mkdir()
    (irs / "AGENTS.md").write_text(
        "---\nname: IRS Agent\ndescription: Tax filing helper\n"
        "suggestions:\n  - How do I file?\n  - What is TDS?\n---\nbody"
    )
    (irs / "branding").mkdir()
    (irs / "branding" / "logo.png").write_bytes(b"png-bytes")

    gp = gateway.plan([irs], load=_loader({str(irs): _settings(irs, 8000)}))
    b = gp.backends[0]
    assert b.display_name == "IRS Agent"
    assert b.description == "Tax filing helper"
    assert b.suggestions == ("How do I file?", "What is TDS?")
    assert b.logo == irs / "branding" / "logo.png"


def test_plan_identity_defaults_are_empty(tmp_path):
    """A hub with no AGENTS.md frontmatter extras and no branding/ dir gets
    safe empty defaults — never an error."""
    a = tmp_path / "a"
    a.mkdir()
    gp = gateway.plan([a], load=_loader({str(a): _settings(a, 8000)}))
    b = gp.backends[0]
    assert b.display_name == "a"          # falls back to folder name
    assert b.description == ""
    assert b.suggestions == ()
    assert b.logo is None


def test_plan_rejects_duplicate_bridge_ports(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    load = _loader({str(a): _settings(a, 8000), str(b): _settings(b, 8000)})
    with pytest.raises(ValueError, match="BRIDGE_PORT"):
        gateway.plan([a, b], load=load)


def test_plan_rejects_duplicate_model_labels(tmp_path):
    """Two hubs resolving to the same model_label would share one OWUI model id:
    provisioning would ACL only the first hub's group and OWUI would route both
    teams' chats to whichever connection wins — silent cross-team exposure.
    Refuse to plan, like the port-collision guard."""
    a = tmp_path / "team-a" / "hub"; a.mkdir(parents=True)
    b = tmp_path / "team-b" / "hub"; b.mkdir(parents=True)
    for h in (a, b):
        (h / "AGENTS.md").write_text("---\nname: Assistant\n---\nbody")
    load = _loader({str(a): _settings(a, 8000), str(b): _settings(b, 8001)})
    with pytest.raises(ValueError, match="model"):
        gateway.plan([a, b], load=load)


def test_plan_model_label_matches_bridge_derivation(tmp_path):
    """The provisioned OWUI model id must equal the id the bridge actually
    serves at /v1/models, or the provisioned entry points at nothing. Both
    sides must use the same fallback chain (loaders._safe_id + server._slugify)
    when AGENTS.md has no name."""
    from hubzoid.loaders.agents import _safe_id
    from hubzoid.server import _slugify as server_slugify

    hub = tmp_path / "Über Hub!"
    hub.mkdir()
    # No AGENTS.md name: both sides must fall back identically.
    gp = gateway.plan([hub], load=_loader({str(hub): _settings(hub, 8000)}))
    assert gp.backends[0].model_label == server_slugify(_safe_id(hub.name))


def test_connection_env_forward_headers_operator_override(tmp_path, monkeypatch):
    """Identity forwarding defaults on (access control needs the email), but an
    operator who explicitly turns it off in the environment keeps that choice —
    connection_env must not hard-force it."""
    irs = tmp_path / "irs"; irs.mkdir()
    monkeypatch.setenv("ENABLE_FORWARD_USER_INFO_HEADERS", "false")
    gp = gateway.plan([irs], load=_loader({str(irs): _settings(irs, 8000)}))
    assert gp.connection_env()["ENABLE_FORWARD_USER_INFO_HEADERS"] == "false"


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


def test_gateway_injects_owui_db_into_bridges(tmp_path, monkeypatch):
    """Each bridge is told where the SHARED gateway DB lives (HUBZOID_OWUI_DB),
    so the restricted-tool group lookup (access.owui_groups) reads the gateway's
    webui.db — not the nonexistent per-hub .openwebui-data/webui.db. Without
    this, restricted-tool access is dead in gateway mode."""
    irs, gpms = tmp_path / "irs", tmp_path / "gpms"
    for h in (irs, gpms):
        h.mkdir()
        (h / "AGENTS.md").write_text("---\nname: x\n---\nbody")

    fake_plan = gateway.GatewayPlan(backends=(
        gateway.GatewayBackend(hub_dir=irs, slug="irs", bridge_port=8000, api_key="irs-key", model_label="irs-agent"),
        gateway.GatewayBackend(hub_dir=gpms, slug="gpms", bridge_port=8001, api_key="gpms-key", model_label="gpms-agent"),
    ))
    monkeypatch.setattr(gateway, "plan", lambda hub_dirs: fake_plan)

    from hubzoid import webui

    def fake_start_gateway(**kwargs):
        proc = MagicMock()
        proc._log_path = tmp_path / "log"
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        return proc
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

    gwdata = tmp_path / "gwdata"
    result = CliRunner().invoke(
        cli.app,
        ["gateway", str(irs), str(gpms), "--data-dir", str(gwdata), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output

    expected = str(gwdata.resolve() / "webui.db")
    bridge_envs = [c["env"] for c in popen_calls if "run" in c["cmd"]]
    assert bridge_envs, "no bridges launched"
    for e in bridge_envs:
        assert e.get("HUBZOID_OWUI_DB") == expected


def _gateway_harness(tmp_path, monkeypatch):
    """Shared fake-process harness for gateway CLI wiring tests."""
    from hubzoid import webui

    irs = tmp_path / "irs"
    irs.mkdir()
    (irs / "AGENTS.md").write_text(
        "---\nname: IRS Agent\ndescription: Tax helper\nsuggestions:\n  - How do I file?\n---\nbody"
    )
    fake_plan = gateway.GatewayPlan(backends=(
        gateway.GatewayBackend(
            hub_dir=irs, slug="irs", bridge_port=8000, api_key="k",
            model_label="irs-agent", display_name="IRS Agent",
            description="Tax helper", suggestions=("How do I file?",),
        ),
    ))
    monkeypatch.setattr(gateway, "plan", lambda hub_dirs: fake_plan)

    def fake_start_gateway(**kwargs):
        proc = MagicMock()
        proc._log_path = tmp_path / "log"
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        return proc
    monkeypatch.setattr(webui, "start_gateway", fake_start_gateway)

    def fake_popen(cmd, env=None, **kw):
        proc = MagicMock()
        proc.poll.return_value = None
        return proc
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)
    return irs


def test_gateway_provisions_when_admin_creds_set(tmp_path, monkeypatch):
    """With HUBZOID_GATEWAY_ADMIN_EMAIL/PASSWORD in the env AND auth on, the
    gateway provisions per-hub OWUI entries after OWUI is up — passing each
    hub's identity from the plan, and allowing first-admin bootstrap only on
    a fresh data dir."""
    from hubzoid import gateway_provision as gwp

    irs = _gateway_harness(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "admin@org.com")
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "s3cret")
    monkeypatch.setenv("WEBUI_AUTH", "true")

    calls = {}

    def fake_provision(*, base_url, email, password, hubs, allow_bootstrap, client=None):
        calls.update(base_url=base_url, email=email, password=password,
                     hubs=hubs, allow_bootstrap=allow_bootstrap)
        return ["irs-agent: created (group 'irs' has read access)"]
    monkeypatch.setattr(gwp, "provision", fake_provision)

    result = CliRunner().invoke(
        cli.app, ["gateway", str(irs), "--data-dir", str(tmp_path / "gw"), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output
    assert calls["email"] == "admin@org.com"
    assert calls["password"] == "s3cret"
    assert calls["allow_bootstrap"] is True    # fresh data dir: no webui.db yet
    spec = calls["hubs"][0]
    assert spec.model_id == "irs-agent"
    assert spec.group == "irs"
    assert spec.suggestions == ("How do I file?",)
    assert "created" in result.output


def test_gateway_no_bootstrap_on_established_data_dir(tmp_path, monkeypatch):
    """A data dir that already has a webui.db is an established gateway: the
    provisioner must not be allowed to create accounts there (a wrong password
    should fail loudly, not quietly sign up a stray user)."""
    from hubzoid import gateway_provision as gwp

    irs = _gateway_harness(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "admin@org.com")
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "s3cret")
    monkeypatch.setenv("WEBUI_AUTH", "true")
    gw = tmp_path / "gw"
    gw.mkdir()
    (gw / "webui.db").write_bytes(b"")   # established

    calls = {}

    def fake_provision(**kwargs):
        calls.update(kwargs)
        return []
    monkeypatch.setattr(gwp, "provision", fake_provision)

    result = CliRunner().invoke(
        cli.app, ["gateway", str(irs), "--data-dir", str(gw), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output
    assert calls["allow_bootstrap"] is False


def test_gateway_provisioning_requires_auth_on(tmp_path, monkeypatch):
    """Admin creds with WEBUI_AUTH off must NOT provision: with auth off,
    OWUI's signin ignores credentials and creates the well-known
    admin@localhost/'admin' account — a security trap. Skip with a message
    that names WEBUI_AUTH."""
    from hubzoid import gateway_provision as gwp

    irs = _gateway_harness(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "admin@org.com")
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "s3cret")
    monkeypatch.delenv("WEBUI_AUTH", raising=False)
    monkeypatch.setattr(gwp, "provision",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not be called")))

    result = CliRunner().invoke(
        cli.app, ["gateway", str(irs), "--data-dir", str(tmp_path / "gw"), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output
    assert "WEBUI_AUTH" in result.output


def test_gateway_provision_skip_message_when_owui_not_ready(tmp_path, monkeypatch):
    """Creds set + OWUI never became ready: the skip message must blame OWUI
    readiness, not tell the operator to set variables they already set."""
    from hubzoid import gateway_provision as gwp

    irs = _gateway_harness(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "admin@org.com")
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "s3cret")
    monkeypatch.setenv("WEBUI_AUTH", "true")
    # Bridges healthy, but the OWUI probe times out.
    monkeypatch.setattr(cli, "_wait_for", lambda url, **kw: "healthz" in url)
    monkeypatch.setattr(gwp, "provision",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not be called")))

    result = CliRunner().invoke(
        cli.app, ["gateway", str(irs), "--data-dir", str(tmp_path / "gw"), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output
    assert "not ready" in result.output.lower()
    assert "set BOTH" not in result.output


def test_gateway_branding_failure_never_kills_boot(tmp_path, monkeypatch):
    """branding.apply hitting e.g. a read-only site-packages must not abort
    the gateway boot — warn and continue."""
    from hubzoid import branding

    irs = _gateway_harness(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise PermissionError("read-only install")
    monkeypatch.setattr(branding, "static_dirs", lambda: [tmp_path / "static"])
    monkeypatch.setattr(branding, "apply", boom)

    result = CliRunner().invoke(
        cli.app, ["gateway", str(irs), "--data-dir", str(tmp_path / "gw"), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output


def test_gateway_skips_provisioning_without_creds(tmp_path, monkeypatch):
    """No admin creds -> provisioning is skipped entirely (today's behavior),
    and the boot proceeds normally."""
    from hubzoid import gateway_provision as gwp

    irs = _gateway_harness(tmp_path, monkeypatch)
    monkeypatch.delenv("HUBZOID_GATEWAY_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(gwp, "provision",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("must not be called")))

    result = CliRunner().invoke(
        cli.app, ["gateway", str(irs), "--data-dir", str(tmp_path / "gw"), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output


def test_gateway_provisioning_failure_never_kills_boot(tmp_path, monkeypatch):
    """Provisioning blowing up (bad creds, OWUI hiccup) logs a warning and the
    gateway keeps running — it must never take the boot down."""
    from hubzoid import gateway_provision as gwp

    irs = _gateway_harness(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "admin@org.com")
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_PASSWORD", "wrong")
    monkeypatch.setenv("WEBUI_AUTH", "true")
    monkeypatch.setattr(gwp, "provision",
                        lambda **kw: (_ for _ in ()).throw(gwp.ProvisionError("bad creds")))

    result = CliRunner().invoke(
        cli.app, ["gateway", str(irs), "--data-dir", str(tmp_path / "gw"), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output
    assert "provision" in result.output.lower()


def test_gateway_applies_its_own_branding(tmp_path, monkeypatch):
    """The gateway stamps its OWN logo into OWUI's static dirs at boot (so the
    chrome logo is deterministic, not a leftover from a prior single-hub run),
    using the gateway baseline CSS that keeps Workspace visible for admins."""
    from hubzoid import branding, webui

    irs = tmp_path / "irs"
    irs.mkdir()
    (irs / "AGENTS.md").write_text("---\nname: x\n---\nbody")

    fake_plan = gateway.GatewayPlan(backends=(
        gateway.GatewayBackend(hub_dir=irs, slug="irs", bridge_port=8000, api_key="k", model_label="irs-agent"),
    ))
    monkeypatch.setattr(gateway, "plan", lambda hub_dirs: fake_plan)

    def fake_start_gateway(**kwargs):
        proc = MagicMock()
        proc._log_path = tmp_path / "log"
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        return proc
    monkeypatch.setattr(webui, "start_gateway", fake_start_gateway)

    def fake_popen(cmd, env=None, **kw):
        proc = MagicMock()
        proc.poll.return_value = None
        return proc
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)

    # Fake OWUI static dir + the gateway's own branding source.
    static = tmp_path / "owui_static"
    static.mkdir()
    monkeypatch.setattr(branding, "static_dirs", lambda: [static])
    gwdata = tmp_path / "gwdata"
    (gwdata / "branding").mkdir(parents=True)
    (gwdata / "branding" / "logo.png").write_bytes(b"GWLOGO")

    result = CliRunner().invoke(
        cli.app, ["gateway", str(irs), "--data-dir", str(gwdata), "--port", "3080"],
    )
    assert result.exit_code == 0, result.output

    # The gateway's logo became OWUI's favicon (deterministic chrome branding).
    assert (static / "favicon.png").read_bytes() == b"GWLOGO"
    # Gateway CSS keeps Workspace visible (admins manage groups/ACLs there).
    assert 'a[href="/workspace"]' not in (static / "custom.css").read_text()
