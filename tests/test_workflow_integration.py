"""Integration: boot the workflow engine against the real HubzoidTestHub and
prove durable execution (run twice; the 2nd run skips completed work).

Runs in a SUBPROCESS because DBOS is a process-global singleton — isolating it
keeps the main test process clean. Model-free (a stub LLM), so it always runs.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TEST_HUB = Path(
    "/Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubzoidTestHub/test-hub"
)

pytestmark = pytest.mark.skipif(
    not (TEST_HUB / "workflows" / "review-prs" / "main.py").exists(),
    reason="HubzoidTestHub workflow not present",
)

_SCRIPT = '''
import os, tempfile
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/hub.db"
os.environ["GITHUB_TOKEN"] = "ghp_test"
from hubzoid.workflows import runtime, context
context.configure(llm=lambda prompt, **kw: "[stub review]")
HUB = %r
runtime.init(HUB, hub_name="test-hub")
runtime.load_workflows(HUB)
runtime.launch()
r1 = runtime.run_now("review_prs")
r2 = runtime.run_now("review_prs")
assert r1 == 2, f"run1 expected 2, got {r1}"
assert r2 == 0, f"run2 expected 0 (durable skip), got {r2}"
# dispatcher marks it due in a 2-minute window
from datetime import datetime, timezone, timedelta
now = datetime(2026,1,1,0,2,0,tzinfo=timezone.utc)
due = runtime.tick(last=now - timedelta(minutes=2), now=now)
assert "review_prs" in due, f"expected due, got {due}"
print("DURABLE_SKIP_OK")
''' % str(TEST_HUB)


def test_workflow_durable_execution():
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True, text=True, timeout=180,
    )
    assert "DURABLE_SKIP_OK" in proc.stdout, (
        f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr[-2000:]}"
    )
