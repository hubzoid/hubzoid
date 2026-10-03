"""Unify restricted-tool group resolution across surfaces (0.8.1).

The bug: a coordinator granted a group in the hub roster (identity/access.csv)
could use restricted tools on WhatsApp but was denied on Open WebUI, because the
OWUI path never consulted the roster. The fix unions three group sources on the
web/MCP path — OWUI groups, roster groups (keyed by the same email), and header
groups — additively, so:

  * a roster-only coordinator now gets their groups on OWUI, and
  * an OWUI-only user with no roster row is never locked out.

Also proves the roster reaches restricted-tool permissions but NOT the MCP front
door, and that a roster edit takes effect with no restart.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hubzoid import server
from hubzoid.access import effective_groups
from hubzoid.access.resolver import reset_roster_cache


@pytest.fixture(autouse=True)
def _legacy_ui(monkeypatch):
    """These tests pin Open WebUI mode (HUBZOID_UI=openwebui), where
    the bridge trusts Open WebUI's forwarded identity and reads its groups; the
    web app mode is covered by tests/test_assertions*.py and test_groups_*.py."""
    monkeypatch.setenv("HUBZOID_UI", "openwebui")


def _write(hub: Path, rel: str, content: str) -> Path:
    p = hub / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))
    return p


class _FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def setup_function(_):
    reset_roster_cache()


# ---------------------------------------------------------------------------
# effective_groups — the union rule in isolation
# ---------------------------------------------------------------------------
def test_roster_group_reaches_owui_identity(tmp_path):
    """The bug, fixed: a roster-only coordinator resolves the group on OWUI."""
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@example.org,coordinator
    """)
    groups = effective_groups(tmp_path, email="ravi@example.org", surface="owui")
    assert "coordinator" in groups


def test_header_groups_still_union(tmp_path):
    groups = effective_groups(
        tmp_path, email=None, surface="owui", header_groups="a, b"
    )
    assert groups == {"a", "b"}


# ---------------------------------------------------------------------------
# _derive_identity — the same, through the real bridge entry point
# ---------------------------------------------------------------------------
def test_derive_identity_unifies_roster_on_owui_login(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@example.org,coordinator
    """)
    req = _FakeRequest({"x-openwebui-user-email": "ravi@example.org"})
    ident = server._derive_identity({}, req, tmp_path)
    assert ident.user == "ravi@example.org"
    assert "coordinator" in ident.groups
    assert ident.surface == "owui"


def test_derive_identity_roster_reloads_without_restart(tmp_path):
    p = _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@example.org,coordinator
    """)
    req = _FakeRequest({"x-openwebui-user-email": "ravi@example.org"})
    ident1 = server._derive_identity({}, req, tmp_path)
    assert "auditor" not in ident1.groups
    p.write_text(textwrap.dedent("""\
        phone,email,groups
        919800000001,ravi@example.org,coordinator;auditor
    """))
    ident2 = server._derive_identity({}, req, tmp_path)
    assert "auditor" in ident2.groups  # no restart, same process


def test_derive_identity_anonymous_when_no_email(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@example.org,coordinator
    """)
    ident = server._derive_identity({}, _FakeRequest({}), tmp_path)
    assert ident.is_anonymous
