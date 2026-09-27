"""Exercise the release shell step without publishing anything."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
RELEASE_STEP = next(s for s in WORKFLOW["jobs"]["publish"]["steps"]
                    if s.get("name") == "GitHub release")["run"]


def run_release(tmp_path, *, exists, publish_status=0, changelog=True):
    scripts = tmp_path / ".github/scripts"
    scripts.mkdir(parents=True)
    shutil.copy(ROOT / ".github/scripts/changelog_section.py", scripts)
    (tmp_path / "CHANGELOG.md").write_text(
        "## 1.0.1\n\nRelease notes.\n" if changelog else "## 1.0.0\nOld notes.\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("hubzoid-1.0.1.whl", "hubzoid-1.0.1.tar.gz"):
        (dist / name).touch()
    env = dict(os.environ, GITHUB_REF_NAME="v1.0.1", RUNNER_TEMP=str(tmp_path),
               PATH=f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    fake_gh = f"""
    gh() {{
      printf '%s\\n' "$*" >> calls.txt
      if [ "$2" = view ]; then return {0 if exists else 1}; fi
      return {publish_status}
    }}
    """
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", fake_gh + RELEASE_STEP],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    return result, (tmp_path / "calls.txt").read_text()


@pytest.mark.parametrize("exists", [True, False])
def test_release_creates_or_uploads_to_existing_release(tmp_path, exists):
    result, calls = run_release(tmp_path, exists=exists)
    assert result.returncode == 0, result.stderr
    assert "dist/hubzoid-1.0.1.whl" in calls
    assert "dist/hubzoid-1.0.1.tar.gz" in calls
    if exists:
        assert "release upload v1.0.1" in calls and "--clobber" in calls
        assert "release create" not in calls
        assert not (tmp_path / "release-notes.md").exists()
    else:
        assert "release create v1.0.1" in calls and "--verify-tag" in calls
        assert (tmp_path / "release-notes.md").read_text().strip() == "Release notes."


@pytest.mark.parametrize("exists", [True, False])
def test_release_does_not_hide_publish_failures(tmp_path, exists):
    result, _ = run_release(tmp_path, exists=exists, publish_status=1)
    assert result.returncode != 0


def test_missing_changelog_prevents_new_release(tmp_path):
    result, calls = run_release(tmp_path, exists=False, changelog=False)
    assert result.returncode != 0
    assert "release create" not in calls
