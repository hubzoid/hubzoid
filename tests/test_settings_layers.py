"""settings.load: configuration layers and their precedence.

    standalone: environment < hub .env < deployment secret < hub secret
                < restricted/.env < restricted secret
    gateway hub: environment < deployment secret (bridge keys only) < hub .env
                < hub secret < restricted/.env < restricted secret

With no secret named, the files load exactly as before.
"""
from __future__ import annotations

import itertools
import logging
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from hubzoid import config_secrets as cs
from hubzoid import deployment, settings
from tests._fake_secrets import clean_process_env, install

KEY = "OTEL_SERVICE_NAME"  # a key a gateway bridge takes from the deployment secret
STANDALONE = ("environment", "hub file", "deployment secret", "hub secret", "restricted file", "restricted secret")
GATEWAY = ("environment", "deployment secret", "hub file", "hub secret", "restricted file", "restricted secret")
SENTINEL = "value-that-must-never-be-logged"


@pytest.fixture(autouse=True)
def _clean():
    with clean_process_env():
        yield


def _hub(tmp_path: Path, name: str = "hub") -> Path:
    hub = tmp_path / name
    (hub / "restricted").mkdir(parents=True, exist_ok=True)
    (hub / "AGENTS.md").write_text("---\nname: x\n---\nbody")
    return hub


def _register(tmp_path: Path, hubs: list[Path], secret: dict | None = None, slugs: dict | None = None) -> Path:
    manifest = tmp_path / "gw" / "deployment.json"
    deployment.save(
        manifest,
        hubs=[dict(key=h.name.lower(), name=h.name, path=str(h.resolve()), model_id=h.name,
                   **({"slug": slugs[h.name]} if slugs else {})) for h in hubs],
        operational_url=f"sqlite:///{tmp_path / 'op.db'}", owui_url="http://127.0.0.1:1",
        owui_db=str(tmp_path / "webui.db"), deployment_secret=secret)
    return manifest


def _write(path: Path, lines: list[str]) -> None:
    if lines:
        path.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("gateway_hub", [False, True], ids=["standalone", "gateway"])
@pytest.mark.parametrize("present", list(itertools.product([False, True], repeat=6)),
                         ids=lambda p: "".join("1" if x else "0" for x in p))
def test_precedence_in_every_combination(tmp_path, monkeypatch, gateway_hub, present):
    order = GATEWAY if gateway_hub else STANDALONE
    on = {layer for layer, flag in zip(order, present) if flag}
    hub = _hub(tmp_path)
    hub_lines, restricted_lines, secrets = [], [], {}
    os.environ["AWS_REGION"] = "eu-west-1"
    if "environment" in on:
        os.environ[KEY] = "environment"
    if "hub file" in on:
        hub_lines.append(f"{KEY}=hub file")
    if "deployment secret" in on:
        secrets["dep"] = {KEY: "deployment secret"}
        if not gateway_hub:
            os.environ["AWS_SECRET_NAME"] = "dep"
    if gateway_hub:
        _register(tmp_path, [hub], {"name": "dep", "region": "eu-west-1"} if "deployment secret" in on else None)
    if "hub secret" in on:
        hub_lines.append("HUBZOID_HUB_SECRET_NAME=hubsec")
        secrets["hubsec"] = {KEY: "hub secret"}
    if "restricted file" in on:
        restricted_lines.append(f"{KEY}=restricted file")
    if "restricted secret" in on:
        restricted_lines.append("HUBZOID_RESTRICTED_SECRET_NAME=rsec")
        secrets["rsec"] = {KEY: "restricted secret"}
    _write(hub / ".env", hub_lines)
    _write(hub / "restricted" / ".env", restricted_lines)
    fake = install(monkeypatch, secrets)

    settings.load(hub)

    winners = [layer for layer in order if layer in on]
    assert os.environ.get(KEY) == (winners[-1] if winners else None)
    assert sorted(fake.names()) == sorted(secrets)  # each named secret read once, nothing else


def test_no_secret_names_behaves_exactly_like_before(tmp_path, monkeypatch):
    """The old algorithm: load_dotenv(hub .env, override) then
    load_dotenv(restricted/.env, override), ${VAR} references included."""
    hub = _hub(tmp_path)
    (hub / ".env").write_text("A=hub\nB=hub\nREF=${BASE}-x\nEMPTY=\nBARE\n")
    (hub / "restricted" / ".env").write_text("B=restricted\nC=${A}-r\n")
    monkeypatch.setattr(cs, "_client", lambda region: pytest.fail("no secret is named"))

    os.environ.update({"A": "env", "BASE": "base", "Z": "env"})
    before = dict(os.environ)
    settings.load(hub)
    new = dict(os.environ)

    os.environ.clear()
    os.environ.update(before)
    load_dotenv(hub / ".env", override=True)
    load_dotenv(hub / "restricted" / ".env", override=True)
    assert new == dict(os.environ)
    assert new["C"] == "hub-r" and new["REF"] == "base-x"


def test_repeated_loads_are_stable(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / ".env").write_text("A=hub\nHUBZOID_HUB_SECRET_NAME=hubsec\n")
    (hub / "restricted" / ".env").write_text("R=file\nHUBZOID_RESTRICTED_SECRET_NAME=rsec\n")
    fake = install(monkeypatch, {"hubsec": {"A": "hub secret"}, "rsec": {"R": "restricted secret"}})
    settings.load(hub)
    first = dict(os.environ)
    settings.load(hub)
    assert dict(os.environ) == first
    assert len(fake.calls) == 2  # read once per process


def test_gateway_hub_takes_only_bridge_keys_from_the_deployment_secret(tmp_path, monkeypatch, caplog):
    hub = _hub(tmp_path)
    _register(tmp_path, [hub], {"name": "dep", "region": "eu-west-1"})
    install(monkeypatch, {"dep": {"WEBUI_SECRET_KEY": "k", "DATABASE_URL": "sqlite:///x.db",
                                  "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY": "e", "OTEL_EXPORTER_OTLP_HEADERS": "h",
                                  "HUBZOID_GATEWAY_ADMIN_EMAIL": "a@x", "GOOGLE_CLIENT_SECRET": SENTINEL,
                                  "ENABLE_SIGNUP": "false"}})
    caplog.set_level(logging.INFO)
    settings.load(hub)
    for key in ("WEBUI_SECRET_KEY", "DATABASE_URL", "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY",
                "OTEL_EXPORTER_OTLP_HEADERS", "HUBZOID_GATEWAY_ADMIN_EMAIL"):
        assert key in os.environ
    assert "GOOGLE_CLIENT_SECRET" not in os.environ and "ENABLE_SIGNUP" not in os.environ
    assert "GOOGLE_CLIENT_SECRET" in caplog.text and SENTINEL not in caplog.text


def test_gateway_hub_ignores_aws_secret_name_in_its_env_file(tmp_path, monkeypatch, caplog):
    hub = _hub(tmp_path)
    _register(tmp_path, [hub])
    (hub / ".env").write_text("AWS_SECRET_NAME=hub-redirect\n")
    fake = install(monkeypatch, {"hub-redirect": {"DATABASE_URL": "postgresql://evil"}})
    caplog.set_level(logging.WARNING)
    settings.load(hub)
    assert fake.calls == []
    assert "DATABASE_URL" not in os.environ
    assert "HUBZOID_HUB_SECRET_NAME" in caplog.text


def test_gateway_launched_bridge_uses_the_values_it_was_given(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    _register(tmp_path, [hub], {"name": "dep", "region": "eu-west-1"})
    os.environ.update({cs.INHERITED_MARKER: "1", "HUBZOID_PUBLIC_URL": "https://h.example/b/hub",
                       "WEBUI_SECRET_KEY": "from-gateway"})
    fake = install(monkeypatch, {"dep": {"HUBZOID_PUBLIC_URL": "https://h.example", "WEBUI_SECRET_KEY": "k"}})
    settings.load(hub)
    assert fake.calls == []
    assert os.environ["HUBZOID_PUBLIC_URL"] == "https://h.example/b/hub"
    assert os.environ["WEBUI_SECRET_KEY"] == "from-gateway"


def test_external_bridge_derives_its_public_url_from_the_deployment_secret(tmp_path, monkeypatch):
    hub = _hub(tmp_path, "Sales.Team")
    _register(tmp_path, [hub], {"name": "dep", "region": "eu-west-1"}, slugs={"Sales.Team": "sales-team-2"})
    install(monkeypatch, {"dep": {"HUBZOID_PUBLIC_URL": "https://h.example/"}})
    settings.load(hub)
    assert os.environ["HUBZOID_PUBLIC_URL"] == "https://h.example/b/sales-team-2"


def test_external_bridge_without_manifest_pointer_uses_its_own_environment(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    _register(tmp_path, [hub])
    os.environ.update({"AWS_SECRET_NAME": "dep", "AWS_REGION": "eu-west-1"})
    fake = install(monkeypatch, {"dep": {"WEBUI_SECRET_KEY": "k", "GOOGLE_CLIENT_SECRET": "g"}})
    settings.load(hub)
    assert fake.calls == [("dep", "eu-west-1")]
    assert os.environ["WEBUI_SECRET_KEY"] == "k" and "GOOGLE_CLIENT_SECRET" not in os.environ


def test_hub_secret_is_named_only_in_the_hub_file(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    os.environ["HUBZOID_HUB_SECRET_NAME"] = "from-env"
    (hub / "restricted" / ".env").write_text("HUBZOID_HUB_SECRET_NAME=from-restricted\n")
    fake = install(monkeypatch, {"from-env": {"A": "1"}, "from-restricted": {"A": "2"}})
    settings.load(hub)
    assert fake.calls == [] and "A" not in os.environ


def test_restricted_secret_is_named_only_in_the_restricted_file(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / ".env").write_text("HUBZOID_RESTRICTED_SECRET_NAME=from-hub\n")
    fake = install(monkeypatch, {"from-hub": {"A": "1"}})
    settings.load(hub)
    assert fake.calls == [] and "A" not in os.environ


def test_secrets_false_loads_files_only(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / ".env").write_text("A=hub\nHUBZOID_HUB_SECRET_NAME=hubsec\nAWS_SECRET_NAME=dep\n")
    (hub / "restricted" / ".env").write_text("R=file\nHUBZOID_RESTRICTED_SECRET_NAME=rsec\n")
    monkeypatch.setattr(cs, "_client", lambda region: pytest.fail("secrets=False never calls AWS"))
    settings.load(hub, secrets=False)
    assert os.environ["A"] == "hub" and os.environ["R"] == "file"
    with cs.fetching_disabled():
        settings.load(hub)


def test_unreadable_secret_stops_the_load_with_name_and_layer(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / "restricted" / ".env").write_text("HUBZOID_RESTRICTED_SECRET_NAME=rsec\n")
    install(monkeypatch, {})
    with pytest.raises(cs.SecretFetchError) as info:
        settings.load(hub)
    assert info.value.name == "rsec" and info.value.layer == "restricted"
    assert "ResourceNotFoundException" in str(info.value)


def test_a_differing_hub_signing_key_warns_but_loads(tmp_path, monkeypatch, caplog):
    hub = _hub(tmp_path)
    _register(tmp_path, [hub], {"name": "dep", "region": "eu-west-1"})
    (hub / ".env").write_text(f"WEBUI_SECRET_KEY={SENTINEL}\n")
    install(monkeypatch, {"dep": {"WEBUI_SECRET_KEY": "deployment-key"}})
    caplog.set_level(logging.WARNING)
    settings.load(hub)
    assert os.environ["WEBUI_SECRET_KEY"] == SENTINEL  # the more specific layer wins
    assert "WEBUI_SECRET_KEY" in caplog.text and "differs" in caplog.text
    assert SENTINEL not in caplog.text and "deployment-key" not in caplog.text


def test_a_secret_overriding_its_file_is_logged_by_name(tmp_path, monkeypatch, caplog):
    hub = _hub(tmp_path)
    (hub / ".env").write_text(f"A=file\nB={SENTINEL}\nHUBZOID_HUB_SECRET_NAME=hubsec\n")
    install(monkeypatch, {"hubsec": {"B": "secret-b", "C": "c"}})
    caplog.set_level(logging.INFO)
    settings.load(hub)
    assert os.environ["B"] == "secret-b"
    assert "hub secret sets B over" in caplog.text
    assert SENTINEL not in caplog.text and "secret-b" not in caplog.text


def test_layer_report_lists_names_and_sources_only(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / ".env").write_text(f"A={SENTINEL}\nB=x\nHUBZOID_HUB_SECRET_NAME=hubsec\n")
    (hub / "restricted" / ".env").write_text(f"B={SENTINEL}\nR=r\n")
    os.environ.update({"A": "env", "AWS_SECRET_NAME": "dep", "AWS_REGION": "eu-west-1"})
    install(monkeypatch, {"dep": {"D": SENTINEL}, "hubsec": {"A": SENTINEL}})
    rows = {r["key"]: r for r in settings.layer_report(hub)}
    assert rows["A"]["layer"] == "hub secret" and rows["A"]["source"] == "aws:hubsec"
    assert rows["A"]["shadows"] == ["environment", "hub"]
    assert rows["B"]["layer"] == "restricted" and rows["B"]["shadows"] == ["hub"]
    assert rows["D"]["layer"] == "deployment secret" and rows["D"]["source"] == "aws:dep"
    assert rows["R"]["source"].endswith("restricted/.env")
    assert SENTINEL not in repr(rows)
    assert "A" not in os.environ or os.environ["A"] == "env"  # the report changes nothing


def test_layer_report_can_skip_the_network(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / ".env").write_text("A=x\nHUBZOID_HUB_SECRET_NAME=hubsec\n")
    monkeypatch.setattr(cs, "_client", lambda region: pytest.fail("fetch_secrets=False"))
    rows = settings.layer_report(hub, fetch_secrets=False)
    assert [r["key"] for r in rows] == ["A", "HUBZOID_HUB_SECRET_NAME"]


# ---------------------------------------------------------------------------
# What other processes get (views over the recorded layers)
# ---------------------------------------------------------------------------
def test_views_drop_hub_only_layers_and_restore_lower_values(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / ".env").write_text("SHARED=hub\nHUB_ONLY=h\nHUBZOID_HUB_SECRET_NAME=hubsec\n")
    (hub / "restricted" / ".env").write_text("SHARED=restricted\nTOOL_TOKEN=t\nWEBUI_SECRET_KEY=w\n")
    os.environ["ENV_ONLY"] = "e"
    install(monkeypatch, {"hubsec": {"HUB_SECRET": "s", "HUB_ONLY": "from-secret"}})
    settings.load(hub)

    children = cs.for_hub_children(os.environ)
    assert children["SHARED"] == "hub" and children["HUB_ONLY"] == "h" and children["ENV_ONLY"] == "e"
    assert "TOOL_TOKEN" not in children and "HUB_SECRET" not in children

    view = cs.deployment_view(os.environ)
    assert "TOOL_TOKEN" not in view and "HUB_SECRET" not in view
    assert view["WEBUI_SECRET_KEY"] == "w"  # an Open WebUI setting in restricted/.env still reaches it

    owui = cs.owui_env({**view, "HUBZOID_GATEWAY_ADMIN_PASSWORD": "p", "AWS_SECRET_NAME": "n",
                        "AWS_ACCESS_KEY_ID": "id"})
    assert "HUBZOID_HUB_SECRET_NAME" not in owui and "HUBZOID_GATEWAY_ADMIN_PASSWORD" not in owui
    assert "AWS_SECRET_NAME" not in owui
    assert "AWS_ACCESS_KEY_ID" not in owui  # a secret was loaded in this process


def test_owui_keeps_aws_settings_when_no_secret_is_used():
    """Open WebUI's own S3 storage may use AWS_* credentials. Without a Hubzoid
    secret they pass through as before."""
    env = {"AWS_ACCESS_KEY_ID": "id", "AWS_REGION": "eu-west-1", "WEBUI_AUTH": "true", "HUBZOID_X": "1"}
    assert cs.owui_env(env) == {"AWS_ACCESS_KEY_ID": "id", "AWS_REGION": "eu-west-1", "WEBUI_AUTH": "true"}
