"""The GitZoid sample with a REAL model: does the review find the planted bugs
and stay quiet on the clean control?

    pytest tests/e2e/test_gitzoid_e2e.py -m e2e -v

Self-skips when the `claude` CLI is not installed. About nine model calls
(claude-local/sonnet unless GITZOID_E2E_MODEL says otherwise).
"""
from __future__ import annotations

import os
import shutil

import pytest

from tests import gitzoid_harness as h

pytestmark = pytest.mark.e2e
REPO = "kestrel-oak/storefront"


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_review_finds_the_planted_bugs(tmp_path, monkeypatch):
    model = os.environ.get("GITZOID_E2E_MODEL", "claude-local/sonnet")
    h.isolate(monkeypatch)
    hub = h.make_hub(tmp_path)
    result = h.run_local(h.wf(hub, "pr_sweep"), hub_dir=hub, trigger="schedule", llm=h.real_llm(model))
    by_pr = {r["pr"].split("#")[1]: r for r in result.value["reviews"]}
    assert set(by_pr) == {"101", "102", "103", "104", "105"}, result.value
    store = h.lib(hub).store.Store(hub / ".hubzoid" / "data")

    def open_findings(n):
        return store.findings_for(REPO, n, ("open",))

    assert any(f["category"] == "bug" and "sync" in f["path"] for f in open_findings(101))      # retries forever
    assert any(f["category"] == "security" and f["path"] == "app/orders.py" for f in open_findings(102))  # no sign-in
    assert any(f["category"] == "security" and f["path"] == "app/search.py" for f in open_findings(103))  # SQL
    assert not [f for f in open_findings(104) if f["severity"] == "high"]                        # clean control
    assert any("gunicorn" in f["body"] for f in open_findings(105))                              # advisory
