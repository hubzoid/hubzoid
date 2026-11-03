"""One tiny real model call through `runtime.reply_check`, on whichever
signed-in CLI this machine has. Skips with no Claude or Codex login."""
from __future__ import annotations

import pytest

from hubzoid import factory_claude, factory_codex, runtime

pytestmark = pytest.mark.e2e


def test_the_signed_in_model_answers(tmp_path, monkeypatch):
    claude, codex = factory_claude.claude_login(), factory_codex.codex_login()
    if claude and claude.signed_in:
        model = "claude-local"
    elif codex and codex.signed_in and not codex.problem:
        model = "codex-local"
    else:
        pytest.skip("no Claude or Codex login on this machine")
    monkeypatch.setenv("MODEL", model)
    (tmp_path / "AGENTS.md").write_text("---\nname: Reply check\n---\nAnswer briefly.")
    assert runtime.reply_check(tmp_path, timeout=90) == (True, "")
