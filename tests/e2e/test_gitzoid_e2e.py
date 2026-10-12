"""The GitZoid sample with a REAL model: does the review find the planted bugs
and stay quiet on the clean control?

    pytest tests/e2e/test_gitzoid_e2e.py -m e2e -v

Self-skips when the selected provider lacks its CLI or API credentials. About nine model calls
(claude-local/sonnet unless GITZOID_E2E_MODEL says otherwise).
"""
from __future__ import annotations

import os
import shutil

import pytest

from tests import gitzoid_harness as h

pytestmark = pytest.mark.e2e
REPO = "kestrel-oak/storefront"


def prerequisite_problem(model: str) -> str | None:
    """Check the selected backend without contacting a provider or exposing keys."""
    provider = model.split("/", 1)[0].lower()
    if provider in ("claude-local", "codex-local"):
        cli = provider.removesuffix("-local")
        return None if shutil.which(cli) else f"{cli} CLI not installed for {model}"
    required = {"openai": ["OPENAI_API_KEY"], "openrouter": ["OPENROUTER_API_KEY"],
                "anthropic": ["ANTHROPIC_API_KEY"], "azure": ["AZURE_API_KEY", "AZURE_API_BASE"]}
    if provider == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return None
    if provider in required:
        missing = [key for key in required[provider] if not os.environ.get(key, "").strip()]
    else:
        from litellm import validate_environment
        missing = validate_environment(model=model).get("missing_keys", [])
    return f"{model}: missing {', '.join(missing)}" if missing else None


def test_review_finds_the_planted_bugs(tmp_path, monkeypatch):
    model = os.environ.get("GITZOID_E2E_MODEL", "claude-local/sonnet")
    problem = prerequisite_problem(model)
    if problem:
        pytest.skip(problem)
    h.isolate(monkeypatch)
    hub = h.make_hub(tmp_path)
    h.write_settings(hub, {"review": {"model": model}})
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
