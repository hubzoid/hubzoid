"""The script the edge injects into Open WebUI (`hubzoid.portal_navigation`).

Static checks only: the browser behaviour is covered by the Console journey
(`portal/tests/journey.cjs`) and the release walkthrough. When Node is present
the script is also parsed, so a syntax error cannot reach every chat page.
"""
from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from hubzoid import edge
from hubzoid.portal_navigation import SCRIPT, inject, script


def test_the_admin_console_link_uses_a_settings_icon_at_the_sidebar_size():
    # Lucide shield-cog, drawn like Open WebUI's own sidebar icons: 16px, 1.5 stroke.
    assert '<circle cx="12.077" cy="11.695" r="3"/>' in SCRIPT
    assert 'M20 13c0 5-3.5 7.5-7.66 8.95' in SCRIPT
    assert 'stroke-width="1.5"' in SCRIPT and "svg { width:16px; height:16px;" in SCRIPT
    # The old panel glyph is gone.
    assert '<rect x="3" y="3" width="18" height="18" rx="3"/>' not in SCRIPT


def test_the_admin_console_link_keeps_its_name_when_collapsed():
    assert "<span>Admin Console</span>" in SCRIPT
    assert "link.setAttribute('aria-label', 'Hubzoid Admin Console');" in SCRIPT
    assert "link.title = 'Hubzoid Admin Console" in SCRIPT
    # Collapsed: the label hides, a keyboard focus still names the link.
    assert "[data-compact] span { display:none; }" in SCRIPT
    assert re.search(r"\[data-compact\]:focus-visible::after \{ content:'Admin Console';", SCRIPT)
    assert "width:32px; height:32px;" in SCRIPT


def test_admin_panel_handling_is_off_unless_users_are_hidden():
    assert "const HIDE_USERS = false;" in script(False)
    assert script(False) == SCRIPT
    hidden = script(True)
    assert "const HIDE_USERS = true;" in hidden and "const HIDE_USERS = false;" not in hidden


def test_the_script_and_the_edge_agree_on_where_the_admin_panel_opens():
    body = script(True)
    assert f"const ADMIN_LANDING = '{edge.ADMIN_LANDING}';" in body
    assert f"const GROUPS = '{edge.GROUPS_URL}';" in body
    # Settings > Integrations is Open WebUI's admin-only dialog tab, over Groups.
    assert edge.ADMIN_LANDING.startswith(edge.GROUPS_URL + "?")
    assert "settings=admin%3Aintegrations" in edge.ADMIN_LANDING


def test_links_to_the_user_list_are_taken_before_open_webui_routes_them():
    body = script(True)
    # A capture-phase click handler, so it runs before Open WebUI's own router.
    assert re.search(r"addEventListener\('click', \(e\) => \{\s*if \(!HIDE_USERS", body)
    assert "}, true);" in body
    # Modified clicks (new tab) are left to the browser and the edge.
    assert "e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey" in body
    # The Admin Panel's own Users tab opens Groups; the menu entry opens the landing.
    assert "!a.closest('nav') ? ADMIN_LANDING : GROUPS" in body
    # Anything else heading for the list is hidden first, then replaced by Groups.
    assert "html[data-hz-leaving] :has(> #users-tabs-container){visibility:hidden !important;}" in body
    assert "go(GROUPS, true)" in body


def test_the_admin_panel_is_never_linked_for_anyone_else():
    """Only Open WebUI renders the Admin Panel entry, and only for its
    administrators. The script adds no admin link of its own: the one link it
    adds goes to the Console, which checks its own access."""
    hrefs = set(re.findall(r"\.href = '([^']+)'", script(True)))
    assert hrefs == {"/portal/"}


def test_inject_adds_the_script_once():
    page = inject(b"<html><body><p>x</p></body></html>")
    assert page.count(b'src="/hubzoid-portal-navigation.js"') == 1
    assert page.endswith(b'defer></script></body></html>')


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
@pytest.mark.parametrize("hide", [False, True])
def test_the_script_parses(tmp_path, hide):
    path = tmp_path / "nav.js"
    path.write_text(script(hide))
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
