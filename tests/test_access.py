"""Tests for hubzoid.access — identity, policy, the tool guard, loader, audit.

Covers the enforcement essence: a restricted tool is hidden from and denied to
a caller without the matching group, the decision is logged, and a hub with no
restricted/ folder is completely unaffected.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents import function_tool
from agents.tool_context import ToolContext

from hubzoid import access
from hubzoid.access import Identity, identity_scope, is_allowed, normalize
from hubzoid.access import audit as auditlib
from hubzoid.access import guard, loader


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _invoke(tool, **kwargs) -> str:
    args = json.dumps(kwargs)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


@function_tool
def sample_tool(store: str = "ALL") -> str:
    """A sample tool used as the thing being guarded."""
    return f"ran:{store}"


# ---------------------------------------------------------------------------
# normalize + identity
# ---------------------------------------------------------------------------
def test_normalize_is_case_insensitive_and_trimmed():
    assert normalize("  Ornate ") == "ornate"
    assert normalize("ERP") == "erp"
    assert normalize("") == ""


def test_identity_make_normalizes_groups_and_surface():
    ident = Identity.make("priya", ["Ornate", " Finance ", "", "ornate"], surface="OWUI")
    assert ident.groups == frozenset({"ornate", "finance"})
    assert ident.surface == "owui"
    assert ident.user == "priya"
    assert not ident.is_anonymous


def test_anonymous_default():
    assert access.ANONYMOUS.is_anonymous
    assert access.current_identity().is_anonymous  # nothing bound


def test_identity_scope_sets_and_restores():
    assert access.current_identity().is_anonymous
    with identity_scope(Identity.make("p", ["ornate"], surface="owui")):
        assert access.current_identity().user == "p"
    assert access.current_identity().is_anonymous  # restored


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------
def test_policy_allows_matching_group():
    owner = Identity.make("priya", ["ornate"], surface="owui")
    assert is_allowed(owner, "ornate") == (True, "group")
    assert is_allowed(owner, "Ornate")[0] is True  # case-insensitive match


def test_policy_denies_without_group():
    owner = Identity.make("priya", ["ornate"], surface="owui")
    assert is_allowed(owner, "erp") == (False, "no-group")


def test_policy_denies_anonymous():
    assert is_allowed(access.ANONYMOUS, "ornate") == (False, "anonymous")


def test_policy_denies_non_owui_surface_even_with_group():
    slack = Identity.make("p", ["ornate"], surface="slack")
    allowed, reason = is_allowed(slack, "ornate")
    assert allowed is False
    assert reason == "surface:slack"


def test_policy_passes_through_unrestricted():
    assert is_allowed(access.ANONYMOUS, "") == (True, "unrestricted")


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------
def test_guard_allows_and_logs_when_permitted(tmp_path):
    guarded = guard.guard_tool(sample_tool, "ornate", tmp_path)
    with identity_scope(Identity.make("priya", ["ornate"], surface="owui")):
        out = _invoke(guarded, store="BLR")
    assert out == "ran:BLR"
    rows = auditlib.read(tmp_path)
    assert rows[-1]["decision"] == "allow"
    assert rows[-1]["tool"] == "sample_tool"
    assert rows[-1]["user"] == "priya"


def test_guard_denies_and_logs_when_not_permitted(tmp_path):
    guarded = guard.guard_tool(sample_tool, "ornate", tmp_path)
    with identity_scope(Identity.make("anjali", ["stock"], surface="owui")):
        out = _invoke(guarded, store="BLR")
    assert "access denied" in out.lower()
    assert "ornate" in out
    rows = auditlib.read(tmp_path)
    assert rows[-1]["decision"] == "deny"
    assert rows[-1]["reason"] == "no-group"


def test_guard_denies_anonymous(tmp_path):
    guarded = guard.guard_tool(sample_tool, "ornate", tmp_path)
    out = _invoke(guarded, store="X")  # no identity bound
    assert "access denied" in out.lower()


def test_guard_is_enabled_reflects_identity(tmp_path):
    guarded = guard.guard_tool(sample_tool, "ornate", tmp_path)
    assert guarded.is_enabled(None, None) is False  # anonymous
    with identity_scope(Identity.make("p", ["ornate"], surface="owui")):
        assert guarded.is_enabled(None, None) is True
    with identity_scope(Identity.make("p", ["erp"], surface="owui")):
        assert guarded.is_enabled(None, None) is False


def test_guard_leaves_original_untouched(tmp_path):
    guard.guard_tool(sample_tool, "ornate", tmp_path)
    # The original sample_tool is the module-level FunctionTool; replace() copies.
    assert sample_tool.is_enabled is True


def test_restricted_surfaces_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "kiosk")
    guarded = guard.guard_tool(sample_tool, "ornate", tmp_path)
    # owui no longer allowed; kiosk is.
    with identity_scope(Identity.make("p", ["ornate"], surface="owui")):
        assert guarded.is_enabled(None, None) is False
    with identity_scope(Identity.make("p", ["ornate"], surface="kiosk")):
        assert guarded.is_enabled(None, None) is True


# ---------------------------------------------------------------------------
# loader + apply
# ---------------------------------------------------------------------------
_RESTRICTED_TOOL_SRC = '''
from agents import function_tool

@function_tool
def ornate_sales(store: str = "ALL") -> str:
    "Ornate sales lookup."
    return "sales:" + store

@function_tool
def ornate_stock(store: str = "ALL") -> str:
    "Ornate stock lookup."
    return "stock:" + store
'''


def _make_restricted_hub(tmp_path: Path) -> Path:
    rdir = tmp_path / "restricted"
    rdir.mkdir()
    (rdir / "ornate.py").write_text(_RESTRICTED_TOOL_SRC)
    (rdir / "_private.py").write_text("X = 1\n")  # ignored (underscore)
    (rdir / ".env").write_text("ORNATE_PASSWORD=secret\n")  # ignored (not .py)
    return tmp_path


def test_load_restricted_tags_permission_by_filename(tmp_path):
    _make_restricted_hub(tmp_path)
    loaded = loader.load_restricted(tmp_path)
    names = {ft.name: perm for ft, perm in loaded}
    assert names == {"ornate_sales": "ornate", "ornate_stock": "ornate"}


def test_load_restricted_empty_without_folder(tmp_path):
    assert loader.load_restricted(tmp_path) == []


def test_apply_unchanged_without_restricted_folder(tmp_path):
    registry = {"a": sample_tool}
    assert access.apply(tmp_path, registry) is registry  # same object, no-op


def test_apply_guards_restricted_tools_end_to_end(tmp_path):
    _make_restricted_hub(tmp_path)
    registry = access.apply(tmp_path, {"existing": sample_tool})
    assert "ornate_sales" in registry and "existing" in registry
    guarded = registry["ornate_sales"]
    # denied anonymous
    assert "access denied" in _invoke(guarded, store="ALL").lower()
    # allowed for an owner in the ornate group
    with identity_scope(Identity.make("priya", ["ornate"], surface="owui")):
        assert _invoke(guarded, store="ALL") == "sales:ALL"


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
def test_audit_writes_month_partitioned_file(tmp_path):
    auditlib.record(tmp_path, user="p", surface="owui", tool="t", decision="allow", reason="group")
    files = list((tmp_path / "logs").glob("access-*.jsonl"))
    assert len(files) == 1
    assert files[0].name.startswith("access-")


def test_audit_read_filters(tmp_path):
    auditlib.record(tmp_path, user="priya", surface="owui", tool="t1", decision="allow", reason="group")
    auditlib.record(tmp_path, user="anjali", surface="owui", tool="t2", decision="deny", reason="no-group")
    assert len(auditlib.read(tmp_path)) == 2
    assert len(auditlib.read(tmp_path, user="priya")) == 1
    assert len(auditlib.read(tmp_path, decision="deny")) == 1
    assert auditlib.read(tmp_path, decision="deny")[0]["user"] == "anjali"


def test_audit_read_missing_is_empty(tmp_path):
    assert auditlib.read(tmp_path) == []


# ---------------------------------------------------------------------------
# Open WebUI group resolution (email -> groups from OWUI's own DB)
# ---------------------------------------------------------------------------
def _make_owui_db(path):
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript(
        '''
        CREATE TABLE "user" (id TEXT, email TEXT);
        CREATE TABLE "group" (id TEXT, name TEXT);
        CREATE TABLE group_member (id TEXT, group_id TEXT, user_id TEXT);
        INSERT INTO "user" VALUES ('u1', 'priya@x.com');
        INSERT INTO "group" VALUES ('g1', 'ornate'), ('g2', 'Finance');
        INSERT INTO group_member VALUES ('m1', 'g1', 'u1'), ('m2', 'g2', 'u1');
        '''
    )
    con.commit()
    con.close()


def test_owui_resolve_groups(tmp_path):
    from hubzoid.access import owui_groups
    data = tmp_path / ".openwebui-data"
    data.mkdir()
    _make_owui_db(data / "webui.db")
    # normalized, so "Finance" -> "finance"
    assert owui_groups.resolve_groups(tmp_path, "priya@x.com") == {"ornate", "finance"}
    assert owui_groups.resolve_groups(tmp_path, "nobody@x.com") == set()
    assert owui_groups.resolve_groups(tmp_path, None) == set()


def test_owui_resolve_missing_db_is_empty(tmp_path):
    from hubzoid.access import owui_groups
    assert owui_groups.resolve_groups(tmp_path, "x@y.com") == set()


# ---------------------------------------------------------------------------
# Enterprise notice: informational, never blocks
# ---------------------------------------------------------------------------
def test_access_unlicensed_warns_but_still_runs(tmp_path, caplog):
    import logging
    _make_restricted_hub(tmp_path)
    with caplog.at_level(logging.WARNING, logger="hubzoid.access"):
        reg = access.apply(tmp_path, {})
    # It does NOT block: the restricted tool is still wired.
    assert "ornate_sales" in reg
    # And it informs that this is an Enterprise feature.
    assert any("Enterprise feature" in r.message for r in caplog.records)


def test_access_licensed_is_quiet(tmp_path, caplog, monkeypatch):
    from hubzoid import licensing
    priv, pub = licensing.generate_keypair()
    token = licensing.issue(
        {"customer": "Acme", "tier": "enterprise", "features": ["access"]}, priv
    )
    monkeypatch.setenv("LICENSE_KEY", token)
    monkeypatch.setenv("HUBZOID_LICENSE_PUBKEY", pub)
    _make_restricted_hub(tmp_path)
    import logging
    with caplog.at_level(logging.WARNING, logger="hubzoid.access"):
        access.apply(tmp_path, {})
    assert not any("Enterprise feature" in r.message for r in caplog.records)


def test_no_restricted_folder_no_notice(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="hubzoid.access"):
        reg = access.apply(tmp_path, {"a": sample_tool})
    assert reg == {"a": sample_tool}
    assert not any("Enterprise" in r.message for r in caplog.records)
