"""Every operator setting the code reads is documented (#45).

A ``HUBZOID_*`` name the package reads by its full name must appear in a doc
under ``docs/`` or in the ``hubzoid/settings.py`` docstring, be a retired
setting the start names (``appmode.RETIRED``), or be one Hubzoid sets for its
own processes (listed below by name). A pure file scan: no hub, no server."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from hubzoid import appmode

ROOT = Path(__file__).resolve().parents[1]
# A complete name. A trailing underscore is a prefix the code completes
# (``HUBZOID_CONNECTOR_<id>_<hash>`` secret references), not a setting.
KEY = re.compile(r"HUBZOID_[A-Z0-9_]*[A-Z0-9]")

# Set by Hubzoid for its own child processes, never by an operator.
INTERNAL_PREFIXES = ("HUBZOID_EDGE_", "HUBZOID_TEST_")
INTERNAL = frozenset({
    "HUBZOID_DEPLOYMENT_FILE", "HUBZOID_HUB_DIR", "HUBZOID_INBOUND_HOST", "HUBZOID_INBOUND_SURFACES",
    "HUBZOID_SESSION_ID", "HUBZOID_SUPERVISED", "HUBZOID_SUPERVISOR_PID",
    # Provider addresses the tests point at a local fake. Named one by one, so a
    # new provider setting is never let through by its shape.
    "HUBZOID_CLICKUP_API_URL", "HUBZOID_GITHUB_API_URL", "HUBZOID_GITHUB_WEB_URL",
    "HUBZOID_GOOGLE_AUTH_URL", "HUBZOID_GOOGLE_TOKEN_URL", "HUBZOID_LINEAR_API_URL",
    "HUBZOID_LINEAR_WEB_URL", "HUBZOID_MS_LOGIN_URL", "HUBZOID_SLACK_API_URL",
    "HUBZOID_WHATSAPP_API_URL",
})


def _read_by_code() -> dict[str, str]:
    """Each name the package uses as a whole string, with one file using it."""
    found: dict[str, str] = {}
    for path in sorted((ROOT / "hubzoid").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and KEY.fullmatch(node.value):
                found.setdefault(node.value, str(path.relative_to(ROOT)))
    return found


def _documented() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "docs").rglob("*.md"):
        names |= set(KEY.findall(path.read_text(encoding="utf-8")))
    settings_doc = ast.get_docstring(ast.parse((ROOT / "hubzoid" / "settings.py").read_text(encoding="utf-8")))
    return names | set(KEY.findall(settings_doc or ""))


def test_every_operator_setting_is_documented():
    documented = _documented()
    missing = {
        name: where for name, where in _read_by_code().items()
        if name not in documented and name not in appmode.RETIRED and name not in INTERNAL
        and not name.startswith(INTERNAL_PREFIXES)
    }
    assert not missing, (
        "Settings the code reads that no doc mentions. Add each to docs/DEPLOYING.md (deployment "
        "settings), the hubzoid/settings.py docstring (hub settings) or the feature's own doc, or to "
        "INTERNAL in this test if Hubzoid sets it itself:\n"
        + "\n".join(f"  {name} (read in {where})" for name, where in sorted(missing.items())))
