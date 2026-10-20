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
                                  "WEBUI_SECRET_KEY", "GOOGLE_CLIENT_SECRET", "PATH", "HUBZOID_SMTP_PASSWORD",
                                  # process control and where model keys go: every subprocess inherits them
                                  "GIT_CONFIG_KEY_0", "GIT_SSH_COMMAND", "BASH_ENV", "PYTHONWARNINGS",
                                  "https_proxy", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
                                  "CLAUDE_CODE_USE_BEDROCK"])
def test_bootstrap_and_operator_names_are_refused(hub, name):
    with pytest.raises(SecretError) as exc:
        put(hub, name)
    assert exc.value.code == "validation_failed" and "operator" in exc.value.message


def test_a_claude_subscription_token_can_be_stored(hub):
    assert put(hub, "CLAUDE_CODE_OAUTH_TOKEN")


def test_a_process_control_name_stored_earlier_never_joins_the_environment(hub, monkeypatch):
    from hubzoid.secretstore import names

    with monkeypatch.context() as m:
        m.setattr(names, "operator_only", lambda name: False)
        put(hub, "GIT_CONFIG_COUNT", "1")
    start_bridge(hub)
    assert "GIT_CONFIG_COUNT" not in os.environ


def test_blank_values_and_restricted_deployment_secrets_are_refused(hub):
    with pytest.raises(SecretError):
        put(hub, "OPENAI_API_KEY", "   ")
    with pytest.raises(SecretError):
        put(hub, "SHARED_TOKEN", scope="deployment", restricted=True)


# ---------------------------------------------------------------------------
# The start-up layer
# ---------------------------------------------------------------------------
def test_a_different_deployment_key_makes_a_secret_unreadable_not_fatal(hub, monkeypatch):
    put(hub, "OPENAI_API_KEY")
    monkeypatch.setenv("HUBZOID_SECRET_KEY", secretbox.Fernet.generate_key().decode())
    start_bridge(hub)
    assert "OPENAI_API_KEY" not in os.environ
    row = next(r for r in report.rows(hub) if r["name"] == "OPENAI_API_KEY")
    assert row["status"] == "unreadable" and row["stored_status"] == layer.UNREADABLE


def test_a_stored_endpoint_never_redirects_an_operators_azure_key(hub, monkeypatch):
    monkeypatch.setenv("AZURE_API_KEY", "operator-key")
    put(hub, "AZURE_API_BASE", "https://elsewhere.example")
    cs.clear_cache()
    start_bridge(hub)
    assert os.environ.get("AZURE_API_BASE") != "https://elsewhere.example"


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
    assert rows["OPENAI_API_KEY"]["source"] == "Stored" and rows["OPENAI_API_KEY"]["expected"] == "model"
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


def test_another_hub_id_is_not_found(hub):
    r = client(hub).get("/api/hubs/otherhub/secrets")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "hub_not_found"


def test_a_removed_and_recreated_secret_never_reuses_a_revision(hub):
    put(hub, "OPENAI_API_KEY")
    store.delete(hub, scope="hub", target="deskhub", name="OPENAI_API_KEY", actor="t", expected_revision=1)
    assert store.list_meta(hub, scope="hub", target="deskhub") == []
    with pytest.raises(SecretError) as exc:
        put(hub, "OPENAI_API_KEY", expected_revision=1)  # a stale editor's revision
    assert exc.value.code == "revision_conflict"
    assert put(hub, "OPENAI_API_KEY", expected_revision="absent").revision == 3
    with pytest.raises(SecretError):
        store.delete(hub, scope="hub", target="deskhub", name="OPENAI_API_KEY", actor="t", expected_revision=1)


def test_a_shared_key_the_model_needs_is_reported_where_it_is_stored(hub):
    (hub / ".env").write_text("MODEL=openai/gpt-5\n")
    put(hub, "OPENAI_API_KEY", scope="deployment")
    row = next(r for r in report.rows(hub) if r["name"] == "OPENAI_API_KEY")
    assert row["level"] == "deployment" and row["stored"]["scope"] == "deployment"


def test_a_blank_aws_value_does_not_lock_a_stored_secret(hub, monkeypatch):
    (hub / ".env").write_text("HUBZOID_HUB_SECRET_NAME=desk/hub\n")
    monkeypatch.setattr(cs, "fetch", lambda name, region, layer: {"ERP_TOKEN": "", "ZOHO_TOKEN": "aws-value"})
    put(hub, "ERP_TOKEN")
    rows = {r["name"]: r for r in report.rows(hub)}
    assert not rows["ERP_TOKEN"]["locked"] and rows["ERP_TOKEN"]["source"] == "Stored"
    assert rows["ZOHO_TOKEN"]["locked"] and rows["ZOHO_TOKEN"]["source"] == "Hub secret"
    assert "desk/hub" not in json.dumps(report.rows(hub))  # never an AWS secret name


def test_a_key_a_person_types_has_one_home_dotenv(hub):
    # Settings, the API and the CLI refuse to store a key a person types: it
    # lives in .env (or the host), so removing a line never revives an old value.
    tc = client(hub)
    r = tc.put("/api/hubs/deskhub/secrets/OPENAI_API_KEY", json={"value": VALUE}, headers=SAME)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "operator_source"
    assert ".env" in r.json()["detail"]["message"]
    from hubzoid.secretstore.cli import secrets_app

    out = CliRunner().invoke(secrets_app, ["set", str(hub), "--name", "OPENAI_API_KEY", "--stdin", "--json"],
                             input=VALUE)
    assert out.exit_code != 0 and "operator_source" in out.output and VALUE not in out.output
    # One stored before this rule is not used, and says where it belongs.
    put(hub, "OPENAI_API_KEY")
    start_bridge(hub)
    assert "OPENAI_API_KEY" not in os.environ
    assert layer.started(hub)[("hub", "OPENAI_API_KEY")][0] == layer.MOVE_TO_ENV
