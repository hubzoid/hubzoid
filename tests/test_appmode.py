"""Retired Open WebUI settings (hubzoid.appmode): advice that fits the hub."""
from __future__ import annotations

from hubzoid import appmode


def test_webui_name_advice_knows_when_the_agent_already_has_a_display_name(tmp_path):
    env = {"WEBUI_NAME": "IRSHub"}
    (tmp_path / "AGENTS.md").write_text("---\nname: irs-hub\ndescription: d\n---\nBody.\n")
    advice = next(r.advice for r in appmode.retired_settings(tmp_path, env) if r.name == "WEBUI_NAME")
    assert "add `display_name:`" in advice
    (tmp_path / "AGENTS.md").write_text("---\nname: irs-hub\ndisplay_name: IRS Hub\ndescription: d\n---\nBody.\n")
    advice = next(r.advice for r in appmode.retired_settings(tmp_path, env) if r.name == "WEBUI_NAME")
    assert "already comes from AGENTS.md" in advice and "add `display_name:`" not in advice
