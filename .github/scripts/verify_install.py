"""Release check: the installed package works and serves the web app and Console.

Run with the Python of a fresh venv that installed the built wheel (not the
source tree). Scaffolds a hub, builds the bridge app and requests the web app
page, the Console page and one of its assets. Also renders a formatted email
using Hubzoid's declared dependencies.

    python .github/scripts/verify_install.py [expected-version]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else None
    have = version("hubzoid")
    if want and have != want.lstrip("v"):
        print(f"version mismatch: tag {want} vs package {have}")
        return 1

    import hubzoid

    pkg = Path(hubzoid.__file__).parent
    if "site-packages" not in pkg.parts:
        print(f"imported from {pkg}, not an installed wheel")
        return 1
    index = pkg / "portal_dist" / "index.html"
    if not index.is_file():
        print("Console missing from the package (hubzoid/portal_dist/index.html)")
        return 1

    from hubzoid.email_delivery import build_message

    message = build_message(
        sender="sender@example.com", recipient="owner@example.com", subject="Report",
        body="## Updates\n\n- **Ready** to review", body_format="markdown", links=[],
        message_id="<install-check@example.com>",
    )
    plain = message.get_body(preferencelist=("plain",)).get_content()
    rich = message.get_body(preferencelist=("html",)).get_content()
    if "Ready" not in plain or "<h2" not in rich or "<strong>Ready</strong>" not in rich:
        print("Formatted email did not retain plain text and rendered Markdown")
        return 1

    import importlib.resources
    if (importlib.resources.files("hubzoid") / "templates").is_dir():
        print("The wheel still ships hub templates")
        return 1
    work = Path(tempfile.mkdtemp())
    subprocess.run([sys.executable, "-m", "hubzoid.cli", "init", "hub"], cwd=work, check=True,
                   stdout=subprocess.DEVNULL)
    if not (work / "hub" / "knowledge" / "house-rules.md").is_file():
        print("hubzoid init did not write the starter")
        return 1
    os.environ.update({
        "HUBZOID_HUB_DIR": str(work / "hub"),
        "MODEL": "openrouter/anthropic/claude-haiku-4.5",
        "OPENROUTER_API_KEY": "not-used",
    })
    from fastapi.testclient import TestClient

    from hubzoid.server import build_app

    client = TestClient(build_app())
    app_page = client.get("/")
    if app_page.status_code != 200 or "<title>" not in app_page.text:
        print(f"Web app page failed: {app_page.status_code}")
        return 1
    page = client.get("/portal/")
    if page.status_code != 200 or "<title>" not in page.text:
        print(f"Console page failed: {page.status_code}")
        return 1
    asset = re.search(r'src="(/portal/assets/[^"]+)"', page.text)
    if not asset or client.get(asset.group(1)).status_code != 200:
        print("Console asset missing")
        return 1
    print(f"ok: hubzoid {have} installed, formatted email rendered, web app and Console served")
    return 0


if __name__ == "__main__":
    sys.exit(main())
