"""Tests for hubzoid.access — identity, policy, the tool guard, loader, audit.

Covers the enforcement essence: a restricted tool is hidden from and denied to
a caller without the matching grant, the decision is logged, and a hub with no
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
def _grant(hub_dir, subject: str, permission: str) -> None:
    access.store_for(hub_dir).grant(subject, Path(hub_dir).name, permission, actor="test")


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
    assert normalize("  ERP ") == "erp"
    assert normalize("ERP") == "erp"
    assert normalize("") == ""


def test_identity_make_normalizes_groups_and_surface():
    ident = Identity.make("priya", ["ERP", " Finance ", "", "erp"], surface="OWUI")
    assert ident.groups == frozenset({"erp", "finance"})
    assert ident.surface == "owui"
    assert ident.user == "priya"
    assert not ident.is_anonymous


def test_anonymous_default():
    assert access.ANONYMOUS.is_anonymous
    assert access.current_identity().is_anonymous  # nothing bound


def test_identity_scope_sets_and_restores():
    assert access.current_identity().is_anonymous
    with identity_scope(Identity.make("p", ["erp"], surface="owui")):
        assert access.current_identity().user == "p"
    assert access.current_identity().is_anonymous  # restored


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------
def test_policy_follows_the_grant_and_never_a_group():
    owner = Identity.make("priya", ["erp"], surface="owui")
    assert is_allowed(owner, "erp", can=lambda: True) == (True, "grant")
    # A group named like the permission grants nothing.
    assert is_allowed(owner, "erp", can=lambda: False) == (False, "no-grant")


def test_policy_denies_anonymous():
    assert is_allowed(access.ANONYMOUS, "erp", can=lambda: True) == (False, "anonymous")


def test_policy_denies_non_owui_surface_even_with_a_grant():
    slack = Identity.make("p", [], surface="slack")
    allowed, reason = is_allowed(slack, "erp", can=lambda: True)
    assert allowed is False
    assert reason == "surface:slack"


def test_policy_passes_through_unrestricted():
    assert is_allowed(access.ANONYMOUS, "", can=lambda: False) == (True, "unrestricted")


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------
def test_guard_allows_and_logs_when_permitted(tmp_path):
    _grant(tmp_path, "priya", "erp")
    guarded = guard.guard_tool(sample_tool, "erp", tmp_path)
    with identity_scope(Identity.make("priya", [], surface="owui")):
        out = _invoke(guarded, store="BLR")
    assert out == "ran:BLR"
    rows = auditlib.read(tmp_path)
    assert rows[-1]["decision"] == "allow"
    assert rows[-1]["tool"] == "sample_tool"
    assert rows[-1]["user"] == "priya"


def test_guard_denies_and_logs_when_not_permitted(tmp_path):
    guarded = guard.guard_tool(sample_tool, "erp", tmp_path)
    with identity_scope(Identity.make("anjali", ["erp"], surface="owui")):
        out = _invoke(guarded, store="BLR")
    assert "access denied" in out.lower()
    assert "erp" in out
    rows = auditlib.read(tmp_path)
    assert rows[-1]["decision"] == "deny"
    assert rows[-1]["reason"] == "no-grant"


def test_guard_is_enabled_reflects_identity(tmp_path):
    _grant(tmp_path, "p", "erp")
    guarded = guard.guard_tool(sample_tool, "erp", tmp_path)
    assert guarded.is_enabled(None, None) is False  # anonymous
    with identity_scope(Identity.make("p", [], surface="owui")):
        assert guarded.is_enabled(None, None) is True
    with identity_scope(Identity.make("q", ["erp"], surface="owui")):
        assert guarded.is_enabled(None, None) is False


def test_guard_leaves_original_untouched(tmp_path):
    guard.guard_tool(sample_tool, "erp", tmp_path)
    # The original sample_tool is the module-level FunctionTool; replace() copies.
    assert sample_tool.is_enabled is True


def test_restricted_surfaces_env_override(tmp_path, monkeypatch):
    _grant(tmp_path, "p", "erp")
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "kiosk")
    guarded = guard.guard_tool(sample_tool, "erp", tmp_path)
    # owui no longer allowed; kiosk is.
    with identity_scope(Identity.make("p", ["erp"], surface="owui")):
        assert guarded.is_enabled(None, None) is False
    with identity_scope(Identity.make("p", ["erp"], surface="kiosk")):
        assert guarded.is_enabled(None, None) is True


# ---------------------------------------------------------------------------
# loader + apply
# ---------------------------------------------------------------------------
_RESTRICTED_TOOL_SRC = '''
from agents import function_tool

@function_tool
def erp_sales(store: str = "ALL") -> str:
    "ERP sales lookup."
    return "sales:" + store

@function_tool
def erp_stock(store: str = "ALL") -> str:
    "ERP stock lookup."
    return "stock:" + store
'''


def _make_restricted_hub(tmp_path: Path) -> Path:
    rdir = tmp_path / "restricted"
    rdir.mkdir()
    (rdir / "erp.py").write_text(_RESTRICTED_TOOL_SRC)
    (rdir / "_private.py").write_text("X = 1\n")  # ignored (underscore)
    (rdir / ".env").write_text("ERP_PASSWORD=secret\n")  # ignored (not .py)
    return tmp_path


def test_load_restricted_tags_permission_by_filename(tmp_path):
    _make_restricted_hub(tmp_path)
    loaded = loader.load_restricted(tmp_path)
    names = {ft.name: perm for ft, perm in loaded}
    assert names == {"erp_sales": "erp", "erp_stock": "erp"}


def test_load_restricted_empty_without_folder(tmp_path):
    assert loader.load_restricted(tmp_path) == []


def test_apply_unchanged_without_restricted_folder(tmp_path):
    registry = {"a": sample_tool}
    assert access.apply(tmp_path, registry) is registry  # same object, no-op


def test_apply_guards_restricted_tools_end_to_end(tmp_path):
    _make_restricted_hub(tmp_path)
    registry = access.apply(tmp_path, {"existing": sample_tool})
    assert "erp_sales" in registry and "existing" in registry
    guarded = registry["erp_sales"]
    # denied anonymous
    assert "access denied" in _invoke(guarded, store="ALL").lower()
    # allowed for an owner granted erp
    _grant(tmp_path, "priya", "erp")
    with identity_scope(Identity.make("priya", [], surface="owui")):
        assert _invoke(guarded, store="ALL") == "sales:ALL"


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
def test_audit_writes_a_database_row_not_a_file(tmp_path):
    assert auditlib.record(tmp_path, user="P", surface="owui", tool="t", decision="allow", reason="group")
    assert not (tmp_path / "logs").exists()
    (row,) = auditlib.read(tmp_path)
    assert (row["user"], row["tool"], row["decision"]) == ("p", "t", "allow")


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
# ---------------------------------------------------------------------------
# Access management wires restricted tools (Apache-2.0, no license gate)
# ---------------------------------------------------------------------------
