"""The Watchtower sample with a REAL model (claude-local), through the CLI a
builder uses: init, run once, read the report, run again.

    pytest tests/e2e/test_watchtower_e2e.py -m e2e -v

Self-skips when the `claude` CLI is not installed. One short model call.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_watchtower_explains_the_checkout_breach(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT")}
    hz = [sys.executable, "-m", "hubzoid"]
    subprocess.run(hz + ["init", "wt", "--template", "watchtower"], cwd=tmp_path, env=env,
                   check=True, capture_output=True)
    hub = tmp_path / "wt"
    with (hub / ".env").open("a") as f:
        f.write("\nMODEL=claude-local/haiku\n")
    run = subprocess.run(hz + ["schedule", "run", str(hub), "watchtower"], env=env,
                         capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    report = (hub / "output" / "watchtower" / "latest.md").read_text()
    assert "## checkout:" in report and "1949 ms" in report
    assert "deploy" in report.lower() or "2026.09.1" in report  # the model tied it to the deploy

    again = subprocess.run(hz + ["schedule", "run", str(hub), "watchtower"], env=env,
                           capture_output=True, text=True, timeout=300)
    assert again.returncode == 0 and "'new': 0" in again.stdout
