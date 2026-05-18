"""Tests for `hubzoid init` scaffolding behavior (Item 1 of the deployment plan).

Covers:
  - Default hub name (`demo-hub`).
  - Hub-folder scaffolding (template files copied).
  - Agents-repo wrapper: parent files written only when parent is fresh.
  - Idempotency: re-running does not overwrite, does not error.
  - The fresh-parent heuristic.
  - `--force` overrides hub-folder skips.
  - `hubzoid init .` skips the parent-wrapper logic (in-place mode).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubzoid.cli import _parent_looks_fresh, app

runner = CliRunner()


def _run_init(tmp_path: Path, *args: str):
    """Invoke `hubzoid init` with cwd set to tmp_path."""
    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        return runner.invoke(app, ["init", *args])
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------------------
# Parent-freshness heuristic
# ---------------------------------------------------------------------------
def test_fresh_parent_when_empty(tmp_path):
    assert _parent_looks_fresh(tmp_path, ignore="anything")


def test_fresh_parent_with_only_dotfiles(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".DS_Store").touch()
    (tmp_path / ".venv").mkdir()
    assert _parent_looks_fresh(tmp_path, ignore="ignored")


def test_fresh_parent_with_readme_and_requirements(tmp_path):
    (tmp_path / "README.md").write_text("# project\n")
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "LICENSE").write_text("MIT\n")
    assert _parent_looks_fresh(tmp_path, ignore="ignored")


def test_not_fresh_when_sibling_hub_exists(tmp_path):
    (tmp_path / "devops-agent").mkdir()
    (tmp_path / "devops-agent" / "AGENTS.md").touch()
    assert not _parent_looks_fresh(tmp_path, ignore="new-hub")


def test_not_fresh_when_random_folder_exists(tmp_path):
    (tmp_path / "src").mkdir()
    assert not _parent_looks_fresh(tmp_path, ignore="ignored")


def test_fresh_when_only_the_target_hub_exists(tmp_path):
    (tmp_path / "demo-hub").mkdir()
    # The hub we are about to create is ignored from the freshness check.
    assert _parent_looks_fresh(tmp_path, ignore="demo-hub")


# ---------------------------------------------------------------------------
# Init: default name + hub folder content
# ---------------------------------------------------------------------------
def test_init_default_creates_demo_hub(tmp_path):
    res = _run_init(tmp_path)
    assert res.exit_code == 0, res.output
    hub = tmp_path / "demo-hub"
    assert (hub / "AGENTS.md").is_file()
    assert (hub / ".env").is_file()
    assert (hub / "knowledge" / "welcome.md").is_file()
    assert (hub / "skills" / "explain-skills" / "SKILL.md").is_file()
    assert (hub / "agents" / "builder" / "AGENTS.md").is_file()


def test_init_named_hub(tmp_path):
    res = _run_init(tmp_path, "devops-agent")
    assert res.exit_code == 0, res.output
    assert (tmp_path / "devops-agent" / "AGENTS.md").is_file()


def test_init_no_dot_env_example(tmp_path):
    """`.env.example` was retired. Scaffold should produce `.env` directly."""
    _run_init(tmp_path)
    hub = tmp_path / "demo-hub"
    assert (hub / ".env").is_file()
    assert not (hub / ".env.example").exists()


# ---------------------------------------------------------------------------
# Init: agents-repo wrapper at parent level
# ---------------------------------------------------------------------------
def test_init_writes_wrapper_when_parent_is_fresh(tmp_path):
    _run_init(tmp_path, "devops-agent")
    assert (tmp_path / "requirements.txt").is_file()
    assert (tmp_path / ".gitignore").is_file()
    assert (tmp_path / "README.md").is_file()
    reqs = (tmp_path / "requirements.txt").read_text()
    assert "hubzoid==" in reqs


def test_init_skips_wrapper_when_parent_is_dirty(tmp_path):
    # Pre-existing sibling folder makes the parent look non-fresh.
    (tmp_path / "src").mkdir()
    _run_init(tmp_path, "devops-agent")
    assert not (tmp_path / "requirements.txt").exists()
    assert not (tmp_path / ".gitignore").exists()
    # README.md was not there to begin with and should not have been added.
    assert not (tmp_path / "README.md").exists()


def test_init_second_run_does_not_overwrite_wrapper(tmp_path):
    _run_init(tmp_path, "devops-agent")
    custom = "# my edits\n"
    (tmp_path / "README.md").write_text(custom)
    (tmp_path / "requirements.txt").write_text("hubzoid==99.0.0\n")

    # Second hub. README and requirements should be preserved verbatim.
    res = _run_init(tmp_path, "support-agent")
    assert res.exit_code == 0, res.output
    assert (tmp_path / "support-agent" / "AGENTS.md").is_file()
    assert (tmp_path / "README.md").read_text() == custom
    assert "99.0.0" in (tmp_path / "requirements.txt").read_text()


def test_init_multi_hub_layout(tmp_path):
    """Samarth-style: multiple `hubzoid init` calls yield sibling hubs."""
    _run_init(tmp_path, "devops-agent")
    _run_init(tmp_path, "support-agent")
    _run_init(tmp_path, "research-agent")
    for name in ("devops-agent", "support-agent", "research-agent"):
        assert (tmp_path / name / "AGENTS.md").is_file()
    # Wrapper written exactly once.
    assert (tmp_path / "requirements.txt").is_file()


# ---------------------------------------------------------------------------
# Idempotency + --force
# ---------------------------------------------------------------------------
def test_init_idempotent_on_existing_hub(tmp_path):
    _run_init(tmp_path, "devops-agent")
    agents_md = (tmp_path / "devops-agent" / "AGENTS.md").read_text()
    # Add a custom edit; second init should NOT overwrite it.
    (tmp_path / "devops-agent" / "AGENTS.md").write_text("# my custom prompt\n")
    res = _run_init(tmp_path, "devops-agent")
    assert res.exit_code == 0, res.output
    assert (tmp_path / "devops-agent" / "AGENTS.md").read_text() == "# my custom prompt\n"
    assert "# my custom prompt" not in agents_md  # sanity: edits diverged from template


def test_init_force_overwrites_hub_files(tmp_path):
    _run_init(tmp_path, "devops-agent")
    (tmp_path / "devops-agent" / "AGENTS.md").write_text("# stale\n")
    res = _run_init(tmp_path, "devops-agent", "--force")
    assert res.exit_code == 0, res.output
    assert (tmp_path / "devops-agent" / "AGENTS.md").read_text().startswith("---")


# ---------------------------------------------------------------------------
# In-place mode (legacy `hubzoid init .`)
# ---------------------------------------------------------------------------
def test_init_dot_skips_parent_wrapper(tmp_path):
    """`hubzoid init .` scaffolds into cwd and does NOT touch the parent."""
    res = _run_init(tmp_path, ".")
    assert res.exit_code == 0, res.output
    assert (tmp_path / "AGENTS.md").is_file()
    # parent of tmp_path is the pytest tmp root; we must not write there.
    assert not (tmp_path.parent / "requirements.txt").exists()
