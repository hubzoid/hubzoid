"""Release check: the installed package works and serves the Console.

Run with the Python of a fresh venv that installed the built wheel (not the
source tree). Scaffolds a hub, builds the bridge app and requests the Console
page and one of its assets.

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

    work = Path(tempfile.mkdtemp())
    subprocess.run([sys.executable, "-m", "hubzoid.cli", "init", "hub"], cwd=work, check=True,
                   stdout=subprocess.DEVNULL)
    os.environ.update({
        "HUBZOID_HUB_DIR": str(work / "hub"),
        "MODEL": "openrouter/anthropic/claude-haiku-4.5",
        "OPENROUTER_API_KEY": "not-used",
    })
    from fastapi.testclient import TestClient

    from hubzoid.server import build_app

    client = TestClient(build_app())
    page = client.get("/portal/")
    if page.status_code != 200 or "<title>" not in page.text:
        print(f"Console page failed: {page.status_code}")
        return 1
    asset = re.search(r'src="(/portal/assets/[^"]+)"', page.text)
    if not asset or client.get(asset.group(1)).status_code != 200:
        print("Console asset missing")
        return 1
    print(f"ok: hubzoid {have} installed, Console served")
    return 0


if __name__ == "__main__":
    sys.exit(main())
