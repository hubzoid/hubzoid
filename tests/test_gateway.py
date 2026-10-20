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

from hubzoid import cli, gateway
from hubzoid.settings import Settings


def _bare(routes):
    """Routes without the hub tag the supervisor's table uses."""
    return [{k: v for k, v in r.items() if k != "hub"} for r in routes]


def _settings(hub_dir: Path, bridge_port: int, keys=("k",), label=None) -> Settings:
    return Settings(
        hub_dir=Path(hub_dir).resolve(),
        model="claude-local",
        model_label=label,
        bridge_api_keys=tuple(keys),
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
def test_plan_edge_routes_namespace_per_hub(tmp_path):
    sales, support = tmp_path / "sales", tmp_path / "support"
    sales.mkdir(); support.mkdir()
    load = _loader({
        str(sales): _settings(sales, 8000),
        str(support): _settings(support, 8001),
    })
    gp = gateway.plan([sales, support], load=load)
    routes = _bare(gp.edge_routes())
    # Each hub's artifacts, Console API, web app API and branding reach its own
    # bridge, with the /b/<slug> prefix stripped. /api/hubs/<key> reaches it by
    # hub key, with the path unchanged.
    expected = []
    for slug, port in (("sales", 8000), ("support", 8001)):
        up = f"http://127.0.0.1:{port}"
        expected += [{"prefix": f"/b/{slug}{part}", "upstream": up, "strip_prefix": f"/b/{slug}"}
                     for part in ("/artifacts", "/portal/api")]
        expected.append({"prefix": f"/api/hubs/{slug}", "upstream": up})
        expected += [{"prefix": f"/b/{slug}{part}", "upstream": up, "strip_prefix": f"/b/{slug}"}
                     for part in ("/api", "/branding", "/v1")]
    assert routes == expected


def test_plan_edge_routes_add_webhooks_for_inbound_hub(tmp_path):
    """A hub whose own .env configures a WhatsApp/Telegram/webhook surface gets a
    /webhooks/<slug> edge route to its inbound server, so the provider's webhook
    is not swallowed by Open WebUI's catch-all. A hub without one gets none, and
    the route carries no strip_prefix (the inbound app serves the full
    /webhooks/<slug>/... path, namespaced by the hub's own slug)."""
    wa, plain = tmp_path / "wa", tmp_path / "plain"
    wa.mkdir(); plain.mkdir()
    (wa / ".env").write_text(
        "WHATSAPP_VERIFY_TOKEN=vt\nWHATSAPP_APP_SECRET=sek\n"
        "WHATSAPP_TOKEN=tok\nWHATSAPP_PHONE_NUMBER_ID=123\n"
    )
    load = _loader({
        str(wa): _settings(wa, 8000, label="wa-agent"),
        str(plain): _settings(plain, 8001, label="plain-agent"),
    })
    gp = gateway.plan([wa, plain], load=load)
    by_slug = {b.slug: b for b in gp.backends}
    assert by_slug["wa"].inbound is True
    assert by_slug["wa"].inbound_port == 8100
    assert by_slug["plain"].inbound is False
    webhooks = [r for r in _bare(gp.edge_routes()) if r["prefix"].startswith("/webhooks")]
    assert webhooks == [{"prefix": "/webhooks/wa", "upstream": "http://127.0.0.1:8100"}]


def test_plan_inbound_honours_custom_port_and_telegram(tmp_path):
    """A Telegram-only surface counts too, and HUBZOID_INBOUND_PORT in the hub's
    own .env moves the /webhooks/<slug> upstream."""
    tg = tmp_path / "tg"; tg.mkdir()
    (tg / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=bt\nTELEGRAM_WEBHOOK_SECRET=ws\n"
        "HUBZOID_INBOUND_PORT=8250\n"
    )
    gp = gateway.plan([tg], load=_loader({str(tg): _settings(tg, 8000, label="tg-agent")}))
    assert gp.backends[0].inbound is True
    assert {"prefix": "/webhooks/tg", "upstream": "http://127.0.0.1:8250"} in _bare(gp.edge_routes())


def test_plan_edge_routes_webhooks_for_every_inbound_hub(tmp_path):
    """Two inbound hubs behind one gateway each get their own /webhooks/<slug>
    route — the pre-0.9.1 singleton (first-inbound-wins) is gone."""
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir(); b.mkdir()
    (a / ".env").write_text(
        "WHATSAPP_VERIFY_TOKEN=vt\nWHATSAPP_APP_SECRET=sek\n"
        "WHATSAPP_TOKEN=tok\nWHATSAPP_PHONE_NUMBER_ID=1\nHUBZOID_INBOUND_PORT=8100\n"
    )
    (b / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=bt\nTELEGRAM_WEBHOOK_SECRET=ws\nHUBZOID_INBOUND_PORT=8101\n"
    )
    load = _loader({
        str(a): _settings(a, 8000, label="alpha-agent"),
        str(b): _settings(b, 8001, label="beta-agent"),
    })
    gp = gateway.plan([a, b], load=load)
    webhooks = [r for r in _bare(gp.edge_routes()) if r["prefix"].startswith("/webhooks")]
    assert webhooks == [
        {"prefix": "/webhooks/alpha", "upstream": "http://127.0.0.1:8100"},
        {"prefix": "/webhooks/beta", "upstream": "http://127.0.0.1:8101"},
    ]


def test_plan_generic_webhook_surface_counts_as_inbound(tmp_path):
    """A hub with only WEBHOOK_INBOUND_SECRET (no WhatsApp/Telegram) is inbound
    and gets its /webhooks/<slug> route."""
    wh = tmp_path / "alerts"; wh.mkdir()
    (wh / ".env").write_text("WEBHOOK_INBOUND_SECRET=shh\nWEBHOOK_INBOUND_NAME=squadcast\n")
    gp = gateway.plan([wh], load=_loader({str(wh): _settings(wh, 8000, label="alerts-agent")}))
    assert gp.backends[0].inbound is True
    assert {"prefix": "/webhooks/alerts", "upstream": "http://127.0.0.1:8100"} in _bare(gp.edge_routes())


def test_plan_rejects_inbound_port_collision(tmp_path):
    """Two inbound hubs sharing HUBZOID_INBOUND_PORT would bind the same loopback
    port; the plan refuses it (mirrors the bridge-port guard)."""
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir(); b.mkdir()
    (a / ".env").write_text("TELEGRAM_BOT_TOKEN=bt\nTELEGRAM_WEBHOOK_SECRET=ws\nHUBZOID_INBOUND_PORT=8100\n")
    (b / ".env").write_text("WEBHOOK_INBOUND_SECRET=shh\nHUBZOID_INBOUND_PORT=8100\n")
    load = _loader({
        str(a): _settings(a, 8000, label="alpha-agent"),
        str(b): _settings(b, 8001, label="beta-agent"),
    })
    with pytest.raises(ValueError, match="HUBZOID_INBOUND_PORT"):
        gateway.plan([a, b], load=load)


def test_plan_gives_inbound_hubs_without_a_port_their_own(tmp_path):
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir(); b.mkdir()
    (a / ".env").write_text("TELEGRAM_BOT_TOKEN=bt\nTELEGRAM_WEBHOOK_SECRET=ws\n")
    (b / ".env").write_text("WEBHOOK_INBOUND_SECRET=shh\n")
    load = _loader({str(a): _settings(a, 8000, label="alpha-agent"), str(b): _settings(b, 8001, label="beta-agent")})
    gp = gateway.plan([a, b], load=load)
    assert [x.inbound_port for x in gp.backends] == [8100, 8101]


def test_plan_public_url_per_hub(tmp_path):
    sales = tmp_path / "sales"; sales.mkdir()
    gp = gateway.plan([sales], load=_loader({str(sales): _settings(sales, 8000)}))
    assert gp.public_url_for("https://hub.example.com/", gp.backends[0]) == "https://hub.example.com/b/sales"


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

def _gateway_harness(tmp_path, monkeypatch):
    """Shared fake-process harness for gateway CLI wiring tests."""
    sales = tmp_path / "sales"
    sales.mkdir()
    (sales / "AGENTS.md").write_text(
        "---\nname: Sales Agent\ndescription: Sales helper\nsuggestions:\n  - How do I place an order?\n---\nbody"
    )
    fake_plan = gateway.GatewayPlan(backends=(
        gateway.GatewayBackend(
            hub_dir=sales, slug="sales", bridge_port=8000, api_key="k",
            model_label="sales-agent", display_name="Sales Agent",
            description="Sales helper", suggestions=("How do I place an order?",),
        ),
    ))
    monkeypatch.setattr(gateway, "plan", lambda hub_dirs, **kw: fake_plan)

    def fake_popen(cmd, env=None, **kw):
        proc = MagicMock()
        proc.poll.return_value = None
        return proc
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *a: None)
    monkeypatch.setattr(cli, "_wait_any", lambda procs, **k: None)
    monkeypatch.setattr(cli, "_stop_groups", lambda procs, **k: list(procs))
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)
    return sales

def test_inherited_deployment_env_takes_only_missing_deployment_keys(tmp_path):
    """Sign-in settings kept in hub .env files still reach the deployment when the
    gateway's own environment lacks them (0.9.x behaviour: last hub listed wins).
    Model keys, Open WebUI's names and anything the gateway already sets stay out."""
    a, b = tmp_path / "a", tmp_path / "b"
    for h in (a, b):
        h.mkdir()
    (a / ".env").write_text("OAUTH_CLIENT_ID=one\nWEBUI_NAME=A\nWEBUI_SECRET_KEY=x\n"
                            "ANTHROPIC_API_KEY=model-key\nENABLE_SIGNUP=false\n")
    (b / ".env").write_text("OAUTH_CLIENT_ID=two\nHUBZOID_PUBLIC_URL=https://hub.example.com\n")

    values, conflicts = gateway.inherited_deployment_env([a, b], {"ENABLE_SIGNUP": "true"})

    assert values == {"OAUTH_CLIENT_ID": "two", "HUBZOID_PUBLIC_URL": "https://hub.example.com"}
    assert conflicts == {"OAUTH_CLIENT_ID": ["a", "b"]}

def test_owned_child_exit_never_counts_as_readiness(monkeypatch):
    proc = MagicMock()
    proc.poll.side_effect = [None, 0]
    monkeypatch.setattr(cli, '_wait_for', lambda *a, **k: True)
    assert not cli._wait_for_bridge(proc, 'http://localhost/healthz', timeout=1)


@pytest.mark.parametrize('code', [0, 1, -9])
def test_supervisor_fails_on_any_unexpected_child_exit(code):
    import typer
    proc = MagicMock()
    proc.poll.return_value = code
    with pytest.raises(typer.Exit) as exc:
        cli._wait_any([proc], interval=0)
    assert exc.value.exit_code == 1


@pytest.mark.parametrize('health', [
    {'status': 'ok'}, {'hub': 'different', 'model': 'sales-agent'},
    {'hub': 'sales', 'model': 'different'},
])
def test_bridge_health_checks_the_hub_and_model(monkeypatch, health):
    import httpx
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: httpx.Response(200, json=health))
    assert not cli._wait_for('http://localhost/healthz', timeout=1,
                             expected_hub='sales', expected_model='sales-agent')
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: httpx.Response(200, json={
        'hub': 'sales', 'model': 'sales-agent'}))
    assert cli._wait_for('http://localhost/healthz', timeout=1,
                        expected_hub='sales', expected_model='sales-agent')


@pytest.mark.parametrize('health', [{'hub': 'sales'}, {'hub': 'sales', 'model': None}])
def test_older_bridge_health_without_model_is_compatible(monkeypatch, health):
    import httpx
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: httpx.Response(200, json=health))
    assert cli._wait_for('http://localhost/healthz', timeout=1,
                         expected_hub='sales', expected_model='sales-agent')


@pytest.mark.parametrize('first', [
    {'hub': 'wrong', 'model': 'sales-agent'},
    {'hub': 'sales', 'model': 'old-model'},
    'temporarily invalid JSON',
])
def test_bridge_identity_mismatch_retries_during_upgrade(monkeypatch, first):
    import httpx
    replies = iter([httpx.Response(200, json=first),
                    httpx.Response(200, json={'hub': 'sales', 'model': 'sales-agent'})])
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: next(replies))
    monkeypatch.setattr(cli.time, 'sleep', lambda *a: None)
    assert cli._wait_for('http://localhost/healthz', timeout=1,
                         expected_hub='sales', expected_model='sales-agent')
