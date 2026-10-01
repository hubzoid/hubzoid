"""`access.effective_groups` in both modes: Hubzoid group names in the web app
mode (in place of Open WebUI group names, which un-migrated hubs read as
restricted-tool permissions), Open WebUI groups in the legacy mode; the roster
and header groups in both; empty on any failure."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

import hubzoid.access as access
import hubzoid.db as db
from hubzoid.access import effective_groups, owui_groups
from hubzoid.access.guard import decide
from hubzoid.access.identity import Identity

ADMIN = "boss@example.org"


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    monkeypatch.delenv("HUBZOID_UI", raising=False)
    hub = tmp_path / "ops"
    (hub / "identity").mkdir(parents=True)
    (hub / "identity" / "access.csv").write_text("phone,email,groups\n+15550001,ann@example.org,field\n")
    (hub / "AGENTS.md").write_text("You help operations.\n")
    eng = create_engine(f"sqlite:///{tmp_path / 'op.db'}")
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    access._stores.clear()
    from hubzoid.access.resolver import reset_roster_cache

    reset_roster_cache()
    gs = access.store_for(hub)
    gs.bootstrap([ADMIN])
    gs.create_group("ERP", actor=ADMIN, emails=["ann@example.org"])
    gs.create_group("Night Shift", actor=ADMIN, emails=["ann@example.org", "bob@example.org"])
    monkeypatch.setattr(owui_groups, "resolve_groups", lambda _hub, _email: {"owui-only"})
    yield hub
    access._stores.clear()


def test_web_app_mode_uses_hubzoid_group_names(hub):
    got = effective_groups(hub, email="Ann@Example.org", surface="web", header_groups="Asserted")
    assert got == {"erp", "night shift", "field", "Asserted"}
    assert "owui-only" not in got
    assert effective_groups(hub, email="bob@example.org") == {"night shift"}
    assert effective_groups(hub, email=None, header_groups=["x"]) == {"x"}


def test_legacy_mode_keeps_open_webui_groups(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    got = effective_groups(hub, email="ann@example.org", header_groups="h")
    assert got == {"owui-only", "field", "h"}


def test_a_blocked_person_has_no_hubzoid_groups(hub):
    access.store_for(hub).suspend("bob@example.org", actor=ADMIN)
    assert effective_groups(hub, email="bob@example.org") == set()


def test_a_store_failure_denies(hub, monkeypatch):
    def boom(_hub):
        raise RuntimeError("no database")

    monkeypatch.setattr(access, "store_for", boom)
    assert effective_groups(hub, email="ann@example.org") == {"field"}   # roster only


def test_an_unmigrated_hub_reads_a_group_name_as_the_permission(hub):
    """Legacy (not Console-managed) access: the restricted tool `erp` is allowed
    for a member of the Hubzoid group named ERP, as it was for the Open WebUI
    group of that name."""
    groups = effective_groups(hub, email="ann@example.org", surface="web")
    ann = Identity.make(user="ann@example.org", groups=groups, surface="web")
    assert decide(hub, ann, "erp") == (True, "group")
    bob = Identity.make(user="bob@example.org",
                        groups=effective_groups(hub, email="bob@example.org"), surface="web")
    assert decide(hub, bob, "erp") == (False, "no-group")
