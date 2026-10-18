"""The secret store (GitHub #49): references, write-only storage, the bridge's
start-up layer below every operator source, the Console API and the CLI.

Every test that stores a value checks it never comes back: not in a response,
an audit row, a log line or CLI output.
"""
from __future__ import annotations

import json
import logging
import os

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from hubzoid import config_secrets as cs
from hubzoid import secretbox
from hubzoid.access import store_for
from hubzoid.secretstore import RefError, SecretError, layer, parse, report, resolve, store
from tests import connectors_fakes as f

APP = "http://127.0.0.1:3599"
SAME = {"origin": APP}
VALUE = "sk-known-value-7f3a9c"
ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    saved = dict(os.environ)
    f.clean_env(monkeypatch)
    for key in ("MODEL", "OPENAI_API_KEY", "ERP_TOKEN", "AZURE_API_KEY", "AZURE_API_BASE",
                "AZURE_API_VERSION", "SHARED_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cs.clear_cache()
    layer.disable()
    secretbox.reset_cache()
    yield
    layer.disable()
    cs.clear_cache()
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def hub(tmp_path):
    return f.make_hub(tmp_path, "deskhub")


def put(hub, name, value=VALUE, *, scope="hub", **kw):
    target = "" if scope == "deployment" else "deskhub"
    return store.put(hub, scope=scope, target=target, name=name, value=value, actor="test", **kw)


def start_bridge(hub):
    """What a bridge does at start: operator layers, then the store."""
    from hubzoid import settings as settingslib

    settingslib.load(hub)
    layer.enable(hub)
    settingslib.load(hub)


def audit_text(hub) -> str:
    with store.engine(hub).connect() as conn:
        rows = conn.execute(text("SELECT * FROM hz_access_audit")).all()
    return json.dumps([list(map(str, r)) for r in rows])


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,scope,target,name", [
    ("secret:deployment/google-oauth", "deployment", "", "google-oauth"),
    ("secret:hub/deskhub/ZOHO_TOKEN", "hub", "deskhub", "ZOHO_TOKEN"),
    ("secret:channel/deskhub/support-mail/password", "channel", "deskhub/support-mail", "password"),
])
def test_references_parse_and_round_trip(raw, scope, target, name):
    ref = parse(raw)
    assert (ref.scope, ref.target, ref.name, str(ref)) == (scope, target, name, raw)
    assert str(parse("env:OPENAI_API_KEY")) == "env:OPENAI_API_KEY"


@pytest.mark.parametrize("raw", ["OPENAI_API_KEY", "vault:x", "secret:hub/x", "secret:org/x",
                                 "secret:hub/Desk Hub/x", "env:not-a-var", "secret:deployment/../x"])
def test_bad_references_say_what_to_write(raw):
    with pytest.raises(RefError) as exc:
        parse(raw)
    assert exc.value.code == "invalid_ref" and str(exc.value)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------
def test_values_are_encrypted_write_only_and_audited_without_the_value(hub, caplog):
    caplog.set_level(logging.DEBUG)
    meta = put(hub, "OPENAI_API_KEY")
    assert meta.revision == 1 and VALUE not in json.dumps(meta.public())
    with store.engine(hub).connect() as conn:
        raw = conn.execute(text("SELECT value_enc FROM hz_secrets")).scalar_one()
    assert VALUE not in raw and secretbox.decrypt_text(hub, raw) == VALUE
    assert "secret_set" in audit_text(hub) and VALUE not in audit_text(hub)
    assert VALUE not in caplog.text
    assert VALUE not in repr(store.values(hub, scope="hub", target="deskhub"))


def test_revisions_guard_concurrent_changes(hub):
    put(hub, "OPENAI_API_KEY", expected_revision="absent")
    with pytest.raises(SecretError) as exc:
        put(hub, "OPENAI_API_KEY", expected_revision="absent")
    assert exc.value.code == "revision_conflict" and exc.value.extra["current_revision"] == 1
    assert put(hub, "OPENAI_API_KEY", "v2", expected_revision=1).revision == 2
    with pytest.raises(SecretError) as exc:
        store.delete(hub, scope="hub", target="deskhub", name="OPENAI_API_KEY", actor="t",
                     expected_revision=1)
    assert exc.value.status == 409
    store.delete(hub, scope="hub", target="deskhub", name="OPENAI_API_KEY", actor="t", expected_revision=2)
    assert store.get_meta(hub, scope="hub", target="deskhub", name="OPENAI_API_KEY") is None


@pytest.mark.parametrize("name", ["HUBZOID_SECRET_KEY", "DATABASE_URL", "BRIDGE_API_KEYS", "AWS_SECRET_ACCESS_KEY",
                                  "WEBUI_SECRET_KEY", "GOOGLE_CLIENT_SECRET", "PATH", "HUBZOID_SMTP_PASSWORD"])
def test_bootstrap_and_operator_names_are_refused(hub, name):
    with pytest.raises(SecretError) as exc:
        put(hub, name)
    assert exc.value.code == "validation_failed" and "operator" in exc.value.message


def test_blank_values_and_restricted_deployment_secrets_are_refused(hub):
    with pytest.raises(SecretError):
        put(hub, "OPENAI_API_KEY", "   ")
    with pytest.raises(SecretError):
        put(hub, "SHARED_TOKEN", scope="deployment", restricted=True)


# ---------------------------------------------------------------------------
# The start-up layer
# ---------------------------------------------------------------------------
def test_a_stored_secret_joins_the_environment_after_a_restart(hub):
    put(hub, "OPENAI_API_KEY")
    from hubzoid import settings as settingslib

    settingslib.load(hub)
    assert "OPENAI_API_KEY" not in os.environ  # not before the bridge turns the store on
    start_bridge(hub)
    assert os.environ["OPENAI_API_KEY"] == VALUE
    put(hub, "OPENAI_API_KEY", "changed", expected_revision=1)
    settingslib.load(hub)  # a reload while serving keeps the start-up snapshot
    assert os.environ["OPENAI_API_KEY"] == VALUE


def test_operator_sources_win_and_blank_lines_do_not(hub):
    (hub / ".env").write_text("OPENAI_API_KEY=from-file\nERP_TOKEN=\n")
    put(hub, "OPENAI_API_KEY")
    put(hub, "ERP_TOKEN")
    start_bridge(hub)
    assert os.environ["OPENAI_API_KEY"] == "from-file"
    assert os.environ["ERP_TOKEN"] == VALUE  # a blank operator line sets nothing
    assert layer.started(hub)[("hub", "OPENAI_API_KEY")][0] == layer.OVERRIDDEN


def test_restricted_secrets_stay_out_of_agent_children(hub):
    put(hub, "ERP_TOKEN", restricted=True)
    put(hub, "OPENAI_API_KEY")
    start_bridge(hub)
    assert os.environ["ERP_TOKEN"] == VALUE
    overrides = cs.child_env_overrides(os.environ)
    assert overrides["ERP_TOKEN"] == "" and "OPENAI_API_KEY" not in overrides
    assert "ERP_TOKEN" not in cs.for_hub_children(os.environ)


def test_a_hub_secret_wins_over_the_deployment_one(hub):
    put(hub, "SHARED_TOKEN", "deployment-value", scope="deployment")
    put(hub, "SHARED_TOKEN", "hub-value")
    start_bridge(hub)
    assert os.environ["SHARED_TOKEN"] == "hub-value"


def test_a_different_deployment_key_makes_a_secret_unreadable_not_fatal(hub, monkeypatch):
    put(hub, "OPENAI_API_KEY")
    monkeypatch.setenv("HUBZOID_SECRET_KEY", secretbox.Fernet.generate_key().decode())
    start_bridge(hub)
    assert "OPENAI_API_KEY" not in os.environ
    row = next(r for r in report.rows(hub) if r["name"] == "OPENAI_API_KEY")
    assert row["status"] == "unreadable" and row["stored_status"] == layer.UNREADABLE


def test_an_azure_key_is_not_sent_to_a_changed_endpoint(hub, monkeypatch):
    from hubzoid.secretstore import names

    monkeypatch.setenv("AZURE_API_BASE", "https://ours.openai.azure.com")
    put(hub, "AZURE_API_KEY", destination=names.destination("AZURE_API_KEY", os.environ))
    monkeypatch.setenv("AZURE_API_BASE", "https://elsewhere.example")
    cs.clear_cache()
    start_bridge(hub)
    assert "AZURE_API_KEY" not in os.environ
    assert layer.started(hub)[("hub", "AZURE_API_KEY")][0] == layer.REBIND


def test_references_resolve_only_this_hubs_secrets(hub):
    put(hub, "ZOHO_TOKEN")
    assert resolve("secret:hub/deskhub/ZOHO_TOKEN", hub) == VALUE
    with pytest.raises(SecretError) as exc:
        resolve("secret:hub/otherhub/ZOHO_TOKEN", hub)
    assert exc.value.code == "permission_denied"
    with pytest.raises(SecretError) as exc:
        resolve("env:NOT_SET_ANYWHERE", hub)
    assert exc.value.code == "secret_unavailable"


# ---------------------------------------------------------------------------
# The report behind list, the Console and doctor
# ---------------------------------------------------------------------------
def test_rows_show_sources_locks_and_expected_names_without_values(hub):
    (hub / ".env").write_text("MODEL=openai/gpt-5\nZOHO_CLIENT_SECRET=x\nBRIDGE_API_KEYS=k\n")
    (hub / ".env.example").write_text("ZOHO_REFRESH_TOKEN=\nZOHO_API_BASE=\n")
    (hub / "restricted").mkdir()
    (hub / "restricted" / ".env").write_text("ODOO_PASSWORD=p\n")
    put(hub, "OPENAI_API_KEY")
    rows = {r["name"]: r for r in report.rows(hub)}
    assert rows["OPENAI_API_KEY"]["source"] == "Console" and rows["OPENAI_API_KEY"]["expected"] == "model"
    assert rows["ZOHO_CLIENT_SECRET"]["locked"] and rows["ZOHO_CLIENT_SECRET"]["source"] == "Hub .env"
    assert rows["ZOHO_REFRESH_TOKEN"]["status"] == "missing"
    assert "ZOHO_API_BASE" not in rows and "BRIDGE_API_KEYS" not in rows and "MODEL" not in rows
    assert rows["ODOO_PASSWORD"]["level"] == "restricted"
    blob = json.dumps(report.rows(hub))
    assert VALUE not in blob and str(hub) not in blob
    shown = report.visible(report.rows(hub), restricted_names=False)
    assert shown["restricted_hidden"] == 1 and "ODOO_PASSWORD" not in json.dumps(shown)


def test_doctor_reports_missing_blank_unused_and_drifting_values(hub, monkeypatch):
    from hubzoid import doctor

    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "deployment-value")
    (hub / ".env").write_text("MODEL=openai/gpt-5\nWEBHOOK_TOKEN=\nLANGFUSE_SECRET_KEY=hub-copy\n"
                              "ERP_TOKEN=from-file\n")
    put(hub, "ERP_TOKEN")
    checks = {c.id: c for c in doctor._secret_checks(hub, fetch_secrets=False, deployment_values={},
                                                     base_env=dict(os.environ))}
    assert checks["secrets.missing"].status == "warn" and "OPENAI_API_KEY" in checks["secrets.missing"].summary
    assert "WEBHOOK_TOKEN" in checks["secrets.blank"].summary
    assert "ERP_TOKEN" in checks["secrets.store"].summary and checks["secrets.store"].status == "warn"
    assert checks["secrets.overrides"].detail == ["LANGFUSE_SECRET_KEY"]
    assert "deployment-value" not in json.dumps([c.summary for c in checks.values()])


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------
def client(hub):
    from fastapi import FastAPI

    from hubzoid.secretstore import routes

    app = FastAPI()
    routes.mount(app, hub)
    return f.client_for(app, APP)


def test_the_local_owner_manages_secrets_and_never_reads_them_back(hub):
    tc = client(hub)
    r = tc.put("/api/hubs/deskhub/secrets/OPENAI_API_KEY", json={"value": VALUE}, headers=SAME)
    assert r.status_code == 200 and r.json()["restart_required"] is True
    listing = tc.get("/api/hubs/deskhub/secrets")
    assert listing.status_code == 200 and VALUE not in listing.text
    row = next(s for s in listing.json()["secrets"] if s["name"] == "OPENAI_API_KEY")
    assert row["source"] == "Console" and row["stored"]["revision"] == 1
    r = tc.put("/api/hubs/deskhub/secrets/OPENAI_API_KEY", json={"value": "x", "expected_revision": 5},
               headers=SAME)
    assert r.status_code == 409 and r.json()["detail"]["current_revision"] == 1
    assert tc.delete("/api/hubs/deskhub/secrets/OPENAI_API_KEY", headers=SAME).status_code == 200
    assert VALUE not in audit_text(hub)


def test_another_hub_id_is_not_found(hub):
    r = client(hub).get("/api/hubs/otherhub/secrets")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "hub_not_found"


def test_a_locked_name_and_a_cross_origin_write_are_refused(hub):
    (hub / ".env").write_text("OPENAI_API_KEY=from-file\n")
    tc = client(hub)
    r = tc.put("/api/hubs/deskhub/secrets/OPENAI_API_KEY", json={"value": VALUE}, headers=SAME)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "secret_locked"
    assert "hub .env" in r.json()["detail"]["message"]
    r = tc.put("/api/hubs/deskhub/secrets/ERP_TOKEN", json={"value": VALUE},
               headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_only_organization_administrators_write_and_see_restricted_names(hub, monkeypatch):
    f.accounts(monkeypatch, hub, {ALICE: ("u-a", "admin"), BOB: ("u-b", "user"), CAROL: ("u-c", "user")})
    gs = store_for(hub)
    gs.bootstrap([ALICE])
    gs.grant(BOB, "deskhub", "manage_access", actor="test")  # this agent's administrator
    put(hub, "ERP_TOKEN", restricted=True)
    put(hub, "OPENAI_API_KEY")
    tc = client(hub)
    alice, bob, carol = ({"x-test-user": who} for who in (ALICE, BOB, CAROL))
    assert tc.get("/api/hubs/deskhub/secrets").status_code == 401
    assert tc.get("/api/hubs/deskhub/secrets", headers=carol).status_code == 404
    seen = tc.get("/api/hubs/deskhub/secrets", headers=bob).json()
    assert "ERP_TOKEN" not in json.dumps(seen) and seen["restricted_hidden"] == 1 and not seen["can_manage"]
    for method in ("put", "delete"):
        kwargs = {"json": {"value": "x"}} if method == "put" else {}
        r = getattr(tc, method)("/api/hubs/deskhub/secrets/ERP_TOKEN", headers={**SAME, **bob}, **kwargs)
        assert r.status_code == 403, method
    assert tc.get("/api/secrets", headers=bob).status_code == 403
    full = tc.get("/api/hubs/deskhub/secrets", headers=alice).json()
    assert "ERP_TOKEN" in json.dumps(full) and full["can_manage"]
    r = tc.put("/api/secrets/SHARED_TOKEN", json={"value": VALUE}, headers={**SAME, **alice})
    assert r.status_code == 200
    assert [s["name"] for s in tc.get("/api/secrets", headers=alice).json()["secrets"]] == ["SHARED_TOKEN"]


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------
def test_the_cli_reads_values_from_stdin_and_never_prints_them(hub):
    from hubzoid.cli import app

    runner = CliRunner()
    r = runner.invoke(app, ["secrets", "set", str(hub), "--name", "OPENAI_API_KEY", "--stdin", "--json"],
                      input=VALUE + "\n")
    assert r.exit_code == 0, r.output
    assert VALUE not in r.output and json.loads(r.output)["secret"]["revision"] == 1
    assert resolve("secret:hub/deskhub/OPENAI_API_KEY", hub) == VALUE  # the trailing newline is dropped
    listing = runner.invoke(app, ["secrets", "list", str(hub), "--json"])
    assert listing.exit_code == 0 and VALUE not in listing.output
    (hub / ".env").write_text("ERP_TOKEN=from-file\n")
    r = runner.invoke(app, ["secrets", "set", str(hub), "--name", "ERP_TOKEN", "--stdin"], input="x\n")
    assert r.exit_code == 4
    r = runner.invoke(app, ["secrets", "delete", str(hub), "--name", "OPENAI_API_KEY", "--check", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["would"] == "delete"
    assert store.get_meta(hub, scope="hub", target="deskhub", name="OPENAI_API_KEY") is not None
