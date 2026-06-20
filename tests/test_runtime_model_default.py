"""When no MODEL is configured anywhere, a hub defaults to the bundled
Claude Agent SDK backend (`claude-local`) instead of erroring at build time.

Backend selection happens in `runtime._resolve_model_id`; we exercise the
resolver directly (no SDK / no network) plus the `describe` view that
`hubzoid doctor` prints.
"""
from __future__ import annotations

import json

import pytest

from hubzoid import runtime as runtime_lib
from hubzoid import settings as settingslib


def _write_main(hub, *, model_frontmatter: str | None = None):
    fm = f"\nmodel: {model_frontmatter}" if model_frontmatter else ""
    (hub / "AGENTS.md").write_text(
        f"---\nname: m\ndescription: d{fm}\n---\nbody"
    )


def test_no_model_anywhere_defaults_to_claude_local(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    _write_main(tmp_path)
    settings = settingslib.load(tmp_path)
    assert settings.model is None
    assert runtime_lib._resolve_model_id(tmp_path, settings) == "claude-local"


def test_env_model_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    _write_main(tmp_path)
    settings = settingslib.load(tmp_path)
    assert (
        runtime_lib._resolve_model_id(tmp_path, settings)
        == "openrouter/anthropic/claude-haiku-4.5"
    )


def test_frontmatter_model_used_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    _write_main(tmp_path, model_frontmatter="openai/gpt-4o-mini")
    settings = settingslib.load(tmp_path)
    assert (
        runtime_lib._resolve_model_id(tmp_path, settings) == "openai/gpt-4o-mini"
    )


def test_describe_reports_claude_local_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    _write_main(tmp_path)
    out = json.loads(runtime_lib.describe(tmp_path))
    assert out == {"backend": "claude-local", "model": "claude-local"}


def test_describe_reports_openai_for_hosted_model(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL", "openai/gpt-4o-mini")
    _write_main(tmp_path)
    out = json.loads(runtime_lib.describe(tmp_path))
    assert out == {"backend": "openai-agents", "model": "openai/gpt-4o-mini"}
