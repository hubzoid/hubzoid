"""`hub.call_jev` follows the same execution-account rules as the other model
calls: it runs as the run's account and stops once that account is blocked or
replaced. Model-free: the Jev seam is a recorder."""
from __future__ import annotations

import pytest

from hubzoid.access import store_for
from hubzoid.workflows import context as wctx
from hubzoid.workflows.context import hub, run_scope
from hubzoid.workflows.identity import IdentityError, resolve

ALICE = "alice@example.org"


@pytest.fixture
def team(tmp_path, monkeypatch):
    for k in ("HUBZOID_WORKFLOW_USER", "HUBZOID_DEPLOYMENT", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WEBUI_AUTH", "true")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    d = tmp_path / "team"
    d.mkdir()
    (d / "AGENTS.md").write_text("---\nname: team\n---\nbody")
    store_for(d).upsert_identity(email=ALICE, owui_id="acct-alice")
    seen = []
    monkeypatch.setattr(wctx, "_JEV", lambda spec, hub_dir=None, subject=None:
                        seen.append(subject) or {"answers": {"ok": {"value": True}}})
    monkeypatch.setattr(wctx, "_JEV_STEP", None)
    return d, seen


def _run_as(hub_dir, who):
    ident = resolve(hub_dir, run_as=who, legacy_subject="workflow:triage")
    return run_scope(hub=hub_dir.name, workflow="triage", hub_dir=hub_dir, engine=None,
                     identity=ident.to_dict())


def test_call_jev_runs_as_the_run_account(team):
    hub_dir, seen = team
    with _run_as(hub_dir, ALICE):
        assert hub.call_jev({"x": 1}, {"ok": {"type": "noul"}}) == {"ok": {"value": True}}
    assert seen == [ALICE]


@pytest.mark.parametrize("change", ["blocked", "replaced"])
def test_call_jev_stops_when_the_account_is_no_longer_usable(team, change):
    hub_dir, seen = team
    with _run_as(hub_dir, ALICE):
        if change == "blocked":
            store_for(hub_dir).suspend(ALICE, actor="admin@example.org")
        else:
            store_for(hub_dir).upsert_identity(email=ALICE, owui_id="acct-someone-else")
        with pytest.raises(IdentityError):
            hub.call_jev({"x": 1}, {"ok": {"type": "noul"}})
    assert seen == []
