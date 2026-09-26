"""Who a scheduled run acts as: precedence, setup default, account checks, no
fallback, legacy compatibility, per-person state. No DBOS, no model, no network.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine

from hubzoid.access import store_for
from hubzoid.access.session import configured_owner
from hubzoid.workflows import identity as idlib
from hubzoid.workflows.identity import IdentityError, RunIdentity
from hubzoid.workflows.state import SHARED, WorkflowState

_ENV = ("WEBUI_AUTH", "HUBZOID_WORKFLOW_USER", "HUBZOID_GATEWAY_ADMIN_EMAIL",
        "WEBUI_ADMIN_EMAIL", "HUBZOID_DEPLOYMENT", "DATABASE_URL")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    d = tmp_path / "sales"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: sales\n---\nbody")
    return d


@pytest.fixture
def shared(hub, monkeypatch):
    """A shared (authenticated) deployment: not the local quickstart."""
    monkeypatch.setenv("WEBUI_AUTH", "true")
    monkeypatch.setenv("HUBZOID_GATEWAY_ADMIN_EMAIL", "owner@company.com")
    return hub


def _account(hub, email, owui_id=None, pending=False):
    store_for(hub).upsert_identity(email=email, owui_id=owui_id or f"id-{email}",
                                   pending=pending)


def _manage(hub, *people):
    gs = store_for(hub)
    gs.set_authoritative(True, hub="sales")
    for p in people:
        gs.grant(p, "sales", "use_hub", actor="test")


# --- the setup default -----------------------------------------------------------

def test_local_quickstart_runs_as_admin_localhost(hub):
    ident = idlib.resolve(hub, legacy_subject="workflow:w")
    assert (ident.subject, ident.source, ident.account_id) == ("admin@localhost", "local", None)
    assert configured_owner(hub) == "admin@localhost"


def test_setup_records_the_configured_owner_once(shared):
    gs = store_for(shared)
    assert configured_owner(shared) == "owner@company.com"
    _account(shared, "owner@company.com")
    assert gs.provision_owner("owner@company.com", "sales")
    assert gs.workflow_default() == "owner@company.com"
    _manage(shared, "owner@company.com")
    # Adding people, admins or hubs later never moves the default.
    _account(shared, "admin2@company.com")
    gs.grant("admin2@company.com", "*", "manage_access", actor="test")
    gs.provision_owner("admin2@company.com", "other")
    assert gs.workflow_default() == "owner@company.com"
    ident = idlib.resolve(shared, legacy_subject="workflow:w")
    assert (ident.subject, ident.source) == ("owner@company.com", "setup")


def test_older_stores_use_the_hubs_recorded_initial_owner(shared):
    gs = store_for(shared)
    with gs._engine.begin() as c:
        gs._meta_set(c, "initial_owner:sales", "owner@company.com")
    _account(shared, "owner@company.com")
    _manage(shared, "owner@company.com")
    assert idlib.resolve(shared).subject == "owner@company.com"


def test_nothing_configured_on_a_managed_hub_names_the_fix(shared):
    _manage(shared)
    with pytest.raises(IdentityError) as err:
        idlib.resolve(shared, legacy_subject="workflow:w", what="Workflow 'w'")
    msg = str(err.value)
    assert "run_as" in msg and "HUBZOID_WORKFLOW_USER" in msg and "owner@company.com" in msg


def test_nothing_configured_on_a_legacy_hub_keeps_the_service_identity(shared, caplog):
    with caplog.at_level(logging.WARNING, logger="hubzoid.workflows"):
        ident = idlib.resolve(shared, legacy_subject="workflow:md:sync")
    assert ident.subject == "workflow:md:sync" and not ident.is_person
    assert "HUBZOID_WORKFLOW_USER" in caplog.text
    with pytest.raises(IdentityError):
        idlib.require_person(ident, "Sending email")


# --- precedence ------------------------------------------------------------------

def test_precedence_run_as_then_hub_then_deployment_then_setup(shared, monkeypatch):
    for e in ("owner@company.com", "deploy@company.com", "hubuser@company.com",
              "priya@company.com"):
        _account(shared, e)
    store_for(shared).provision_owner("owner@company.com", "sales")
    _manage(shared, *("owner@company.com", "deploy@company.com", "hubuser@company.com",
                      "priya@company.com"))
    assert idlib.resolve(shared).source == "setup"
    monkeypatch.setenv("HUBZOID_WORKFLOW_USER", "deploy@company.com")
    assert (idlib.resolve(shared).subject, idlib.resolve(shared).source) == (
        "deploy@company.com", "deployment")
    (shared / ".env").write_text("HUBZOID_WORKFLOW_USER=hubuser@company.com\n")
    assert (idlib.resolve(shared).subject, idlib.resolve(shared).source) == (
        "hubuser@company.com", "hub")
    # The bridge has loaded the hub .env over the environment: same answer.
    monkeypatch.setenv("HUBZOID_WORKFLOW_USER", "hubuser@company.com")
    assert idlib.resolve(shared).source == "hub"
    ident = idlib.resolve(shared, run_as="Priya@Company.com")
    assert (ident.subject, ident.source) == ("priya@company.com", "run_as")


def test_run_as_is_validated_at_declaration():
    assert idlib.validate_run_as(" Priya@Company.com ") == "priya@company.com"
    for bad in ("priya", "", None, 7, "a b@c.com"):
        with pytest.raises(ValueError):
            idlib.validate_run_as(bad)


# --- the account must be usable; never a fallback ----------------------------------

def test_unknown_pending_blocked_and_unentitled_accounts_fail(shared):
    _account(shared, "owner@company.com")
    store_for(shared).provision_owner("owner@company.com", "sales")
    with pytest.raises(IdentityError, match="no signed-in account"):
        idlib.resolve(shared, run_as="ghost@company.com")
    _account(shared, "new@company.com", pending=True)
    with pytest.raises(IdentityError, match="awaiting approval"):
        idlib.resolve(shared, run_as="new@company.com")
    _account(shared, "gone@company.com")
    store_for(shared).suspend("gone@company.com", actor="test")
    with pytest.raises(IdentityError, match="blocked"):
        idlib.resolve(shared, run_as="gone@company.com")
    _account(shared, "outsider@company.com")
    _manage(shared, "owner@company.com")
    with pytest.raises(IdentityError, match="no access"):
        idlib.resolve(shared, run_as="outsider@company.com")


def test_a_failed_account_never_falls_back_to_the_default(shared):
    _account(shared, "owner@company.com")
    store_for(shared).provision_owner("owner@company.com", "sales")
    _account(shared, "priya@company.com")
    store_for(shared).suspend("priya@company.com", actor="test")
    with pytest.raises(IdentityError):
        idlib.resolve(shared, run_as="priya@company.com", legacy_subject="workflow:w")


def test_recheck_stops_a_run_whose_account_was_blocked_or_replaced(shared):
    _account(shared, "priya@company.com", owui_id="acct-1")
    ident = idlib.resolve(shared, run_as="priya@company.com")
    idlib.recheck(shared, "sales", ident)
    # The email now belongs to a different account: stop, never continue as it.
    store_for(shared).upsert_identity(email="priya@company.com", owui_id="acct-2")
    with pytest.raises(IdentityError):
        idlib.recheck(shared, "sales", ident)


def test_identity_round_trips_as_plain_data(shared):
    _account(shared, "priya@company.com")
    ident = idlib.resolve(shared, run_as="priya@company.com")
    assert RunIdentity.from_dict(ident.to_dict()) == ident


# --- legacy workflow:* grants are reported, not carried over ----------------------------

def test_legacy_grants_are_reported_not_granted(shared, caplog):
    _account(shared, "owner@company.com")
    _manage(shared, "owner@company.com")
    gs = store_for(shared)
    gs.grant("workflow:nightly", "sales", "crm_read", actor="test")
    with caplog.at_level(logging.WARNING, logger="hubzoid.workflows"):
        ident = idlib.resolve(shared, run_as="owner@company.com",
                              legacy_subject="workflow:nightly")
    assert ident.legacy_permissions == ("crm_read",)
    assert "crm_read" in caplog.text
    assert not gs.can("owner@company.com", "sales", "crm_read")
    assert gs.can("workflow:nightly", "sales", "crm_read")  # untouched


# --- per-person state ----------------------------------------------------------------

def test_state_is_per_person_and_older_state_stays_unassigned(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    legacy = WorkflowState(eng, "sales", "digest")          # written before identities
    legacy["cursor"] = 41
    alice = WorkflowState(eng, "sales", "digest", owner="alice@x.com")
    bob = WorkflowState(eng, "sales", "digest", owner="bob@x.com")
    assert alice.get("cursor") is None and bob.get("cursor") is None   # starts separately
    alice["cursor"] = 1
    bob["cursor"] = 7
    assert (alice["cursor"], bob["cursor"], legacy["cursor"]) == (1, 7, 41)  # kept as it was
    shared = WorkflowState(eng, "sales", "digest", owner=SHARED)
    shared["etag"] = "v1"
    assert "etag" not in alice and "etag" not in bob and "etag" not in legacy


def test_markdown_scratch_is_per_person_and_leaves_the_old_folder_alone(hub):
    alice = RunIdentity("alice@x.com", "a", "run_as", "alice@x.com")
    bob = RunIdentity("bob@x.com", "b", "run_as", "bob@x.com")
    legacy = RunIdentity("workflow:md:sync", None, "legacy-service")
    # An upgraded hub: the task's folder already holds state from before.
    (hub / ".hubzoid/schedule/sync").mkdir(parents=True)
    (hub / ".hubzoid/schedule/sync/state.json").write_text('{"sha": "abc"}')
    assert idlib.markdown_scratch(hub, "sync", legacy) == ".hubzoid/schedule/sync"
    mine = idlib.markdown_scratch(hub, "sync", alice)
    other = idlib.markdown_scratch(hub, "sync", bob)
    assert mine.startswith(".hubzoid/schedule/sync@alice-x.com-")
    assert other.startswith(".hubzoid/schedule/sync@bob-x.com-") and other != mine
    # Siblings, never nested: no person's writable root covers another's or the old one.
    assert not mine.startswith(".hubzoid/schedule/sync/")
    assert (hub / ".hubzoid/schedule/sync/state.json").read_text() == '{"sha": "abc"}'
    assert not (hub / ".hubzoid/schedule/sync/.owner").exists()


def test_a_legacy_hub_never_switches_on_the_setup_default(shared, caplog):
    """Recording an owner (their first verified sign-in) must not change how a
    legacy hub's tasks run: same service identity, same old scratch folder."""
    _account(shared, "owner@company.com")
    gs = store_for(shared)
    assert gs.provision_owner("owner@company.com", "sales")        # not fresh: stays legacy
    assert not gs.is_authoritative("sales")
    assert gs.workflow_default() == "owner@company.com"
    ident = idlib.resolve(shared, legacy_subject="workflow:md:sync")
    assert (ident.subject, ident.source) == ("workflow:md:sync", "legacy-service")
    assert idlib.markdown_scratch(shared, "sync", ident) == ".hubzoid/schedule/sync"
    # Explicit configuration still switches it.
    (shared / ".env").write_text("HUBZOID_WORKFLOW_USER=owner@company.com\n")
    assert idlib.resolve(shared, legacy_subject="workflow:md:sync").source == "hub"


# --- one resolution for execution, listing and display (release review) -------------

def _secret_hub(shared, monkeypatch, env_line: str, secret: dict | None):
    from hubzoid import config_secrets
    from tests import _fake_secrets

    config_secrets.clear_cache()
    fake = _fake_secrets.install(monkeypatch, {"prod/sales": secret} if secret is not None else {})
    (shared / ".env").write_text(env_line + "HUBZOID_HUB_SECRET_NAME=prod/sales\n")
    wf = shared / "workflows" / "daily"
    wf.mkdir(parents=True)
    (wf / "main.py").write_text("from hubzoid import workflow\n\n@workflow()\ndef daily():\n    return 1\n")
    return fake


def _listed(hub):
    from hubzoid.workflows.observe import catalog

    return next(r for r in catalog(hub) if r["name"] == "daily")["runs_as"]


def test_a_hub_secret_wins_over_the_hub_env_everywhere(shared, monkeypatch):
    for e in ("hubuser@company.com", "secretuser@company.com"):
        _account(shared, e)
    _manage(shared, "hubuser@company.com", "secretuser@company.com")
    _secret_hub(shared, monkeypatch, "HUBZOID_WORKFLOW_USER=hubuser@company.com\n",
                {"HUBZOID_WORKFLOW_USER": "secretuser@company.com"})
    ident = idlib.resolve(shared, legacy_subject="workflow:daily")
    assert (ident.subject, ident.source) == ("secretuser@company.com", "hub-secret")
    listed = _listed(shared)
    assert (listed["account"], listed["source"]) == ("secretuser@company.com", "hub-secret")
    assert listed["via"] == "HUBZOID_WORKFLOW_USER in the hub secret"
    assert idlib.describe(shared, legacy_subject="workflow:daily") == (
        "runs as secretuser@company.com (HUBZOID_WORKFLOW_USER in the hub secret)")


def test_listing_matches_execution_when_only_a_secret_sets_it(shared, monkeypatch):
    """The listing used to show the setup/local default while a run used the
    secret's account, labelled as the deployment's."""
    _account(shared, "secretuser@company.com")
    _manage(shared, "secretuser@company.com")
    _secret_hub(shared, monkeypatch, "", {"HUBZOID_WORKFLOW_USER": "secretuser@company.com"})
    listed = _listed(shared)
    ran = idlib.resolve(shared, legacy_subject="workflow:daily")
    assert (listed["account"], listed["source"]) == (ran.subject, ran.source) == (
        "secretuser@company.com", "hub-secret")


def test_an_unreadable_hub_secret_is_an_error_not_a_fallback(shared, monkeypatch):
    _account(shared, "hubuser@company.com")
    _manage(shared, "hubuser@company.com")
    _secret_hub(shared, monkeypatch, "HUBZOID_WORKFLOW_USER=hubuser@company.com\n", None)
    with pytest.raises(IdentityError, match="HUBZOID_WORKFLOW_USER could not be read"):
        idlib.resolve(shared, legacy_subject="workflow:daily")
    listed = _listed(shared)
    assert listed["account"] is None and "could not be read" in listed["error"]


def test_the_gateway_records_its_workflow_user_for_cli_commands(shared, tmp_path):
    """A CLI job does not inherit the gateway's environment; the manifest
    carries the deployment's HUBZOID_WORKFLOW_USER so listing and runs agree."""
    from hubzoid import deployment

    _account(shared, "deploy@company.com")
    _manage(shared, "deploy@company.com")
    deployment.save(tmp_path / "gw" / "deployment.json",
                    hubs=[dict(key="sales", name="sales", path=str(shared), model_id="sales")],
                    operational_url=f"sqlite:///{tmp_path / 'ops.db'}",
                    owui_url="http://127.0.0.1:9", owui_db=str(tmp_path / "gw" / "webui.db"),
                    workflow_user="Deploy@Company.com")
    ident = idlib.resolve(shared, legacy_subject="workflow:daily")
    assert (ident.subject, ident.source) == ("deploy@company.com", "deployment")


def test_a_legacy_hub_switches_only_on_explicit_configuration_including_a_secret(shared, monkeypatch):
    _account(shared, "secretuser@company.com")
    _secret_hub(shared, monkeypatch, "", {})             # a named secret without the key
    ident = idlib.resolve(shared, legacy_subject="workflow:md:sync")
    assert (ident.subject, ident.source) == ("workflow:md:sync", "legacy-service")
    from hubzoid import config_secrets

    config_secrets.clear_cache()
    _fake = __import__("tests._fake_secrets", fromlist=["install"])
    _fake.install(monkeypatch, {"prod/sales": {"HUBZOID_WORKFLOW_USER": "secretuser@company.com"}})
    ident = idlib.resolve(shared, legacy_subject="workflow:md:sync")
    assert (ident.subject, ident.source) == ("secretuser@company.com", "hub-secret")
