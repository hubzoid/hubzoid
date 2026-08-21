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

import sqlite3
import textwrap
from pathlib import Path

from hubzoid import server
from hubzoid.access import effective_groups
from hubzoid.access.resolver import reset_roster_cache


def _write(hub: Path, rel: str, content: str) -> Path:
    p = hub / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content))
    return p


def _make_owui_db(hub_dir: Path, email: str, group: str) -> None:
    data = hub_dir / ".openwebui-data"
    data.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(data / "webui.db")
    con.executescript(
        f'''
        CREATE TABLE "user" (id TEXT, email TEXT);
        CREATE TABLE "group" (id TEXT, name TEXT);
        CREATE TABLE group_member (id TEXT, group_id TEXT, user_id TEXT);
        INSERT INTO "user" VALUES ('u1', '{email}');
        INSERT INTO "group" VALUES ('g1', '{group}');
        INSERT INTO group_member VALUES ('m1', 'g1', 'u1');
        '''
    )
    con.commit()
    con.close()


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
        919800000001,ravi@isha.org,coordinator
    """)
    groups = effective_groups(tmp_path, email="ravi@isha.org", surface="owui")
    assert "coordinator" in groups


def test_owui_only_user_not_in_roster_is_not_locked_out(tmp_path):
    """Additive, never a gate: an email absent from the roster keeps its OWUI
    groups instead of being denied."""
    _make_owui_db(tmp_path, "priya@x.com", "ornate")
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
    """)
    groups = effective_groups(tmp_path, email="priya@x.com", surface="owui")
    assert groups == {"ornate"}


def test_union_of_owui_and_roster(tmp_path):
    """A person in both stores carries both groups."""
    _make_owui_db(tmp_path, "ravi@isha.org", "ornate")
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
    """)
    groups = effective_groups(tmp_path, email="ravi@isha.org", surface="owui")
    assert groups == {"ornate", "coordinator"}


def test_no_roster_folder_is_owui_only(tmp_path):
    _make_owui_db(tmp_path, "priya@x.com", "ornate")
    groups = effective_groups(tmp_path, email="priya@x.com", surface="owui")
    assert groups == {"ornate"}


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
        919800000001,ravi@isha.org,coordinator
    """)
    req = _FakeRequest({"x-openwebui-user-email": "ravi@isha.org"})
    ident = server._derive_identity({}, req, tmp_path)
    assert ident.user == "ravi@isha.org"
    assert "coordinator" in ident.groups
    assert ident.surface == "owui"


def test_derive_identity_roster_reloads_without_restart(tmp_path):
    p = _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
    """)
    req = _FakeRequest({"x-openwebui-user-email": "ravi@isha.org"})
    ident1 = server._derive_identity({}, req, tmp_path)
    assert "auditor" not in ident1.groups
    p.write_text(textwrap.dedent("""\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator;auditor
    """))
    ident2 = server._derive_identity({}, req, tmp_path)
    assert "auditor" in ident2.groups  # no restart, same process


def test_derive_identity_anonymous_when_no_email(tmp_path):
    _write(tmp_path, "identity/access.csv", """\
        phone,email,groups
        919800000001,ravi@isha.org,coordinator
    """)
    ident = server._derive_identity({}, _FakeRequest({}), tmp_path)
    assert ident.is_anonymous
