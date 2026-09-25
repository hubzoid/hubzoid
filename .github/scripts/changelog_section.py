"""Print the CHANGELOG.md section for a version tag (vX.Y.Z), for release notes."""
from __future__ import annotations

import re
import sys
from pathlib import Path

tag = sys.argv[1].lstrip("v")
text = Path(__file__).resolve().parents[2].joinpath("CHANGELOG.md").read_text()
match = re.search(rf"^## \[?{re.escape(tag)}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
if not match:
    sys.exit(f"CHANGELOG.md has no section for {tag}")
print(match.group(1).strip())
