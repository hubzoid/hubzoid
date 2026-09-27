"""Exercise the release shell step without publishing anything."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
RELEASE_STEP = next(s for s in WORKFLOW["jobs"]["github-release"]["steps"]
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


def test_only_publishing_a_release_triggers_ci():
    workflows = list((ROOT / ".github/workflows").glob("*.y*ml"))
    assert workflows == [ROOT / ".github/workflows/ci.yml"]
    # BaseLoader preserves GitHub's YAML 1.2 'on' key (not YAML 1.1 True).
    config = yaml.load(workflows[0].read_text(), Loader=yaml.BaseLoader)
    assert config["on"] == {"release": {"types": ["published"]}}


def test_all_release_checks_gate_publishing_and_publish_jobs_retry_independently():
    jobs = WORKFLOW["jobs"]
    assert set(jobs["publish-image"]["needs"]) == {"validate", "image"}
    assert jobs["publish-pypi"]["needs"] == "publish-image"
    assert jobs["github-release"]["needs"] == "publish-pypi"
    for name in ("publish-image", "publish-pypi", "github-release"):
        assert "if" not in jobs[name]  # retain GitHub's dependency-success gate
    checks = "\n".join(s.get("run", "") for s in jobs["validate"]["steps"])
    for required in ("pytest", "npm run lint", "npm test", "verify_install.py"):
        assert required in checks
    assert jobs["publish-pypi"]["environment"]["name"] == "pypi"
    assert jobs["publish-pypi"]["permissions"]["id-token"] == "write"
    assert all("docker/" not in s.get("uses", "")
               for name in ("publish-pypi", "github-release") for s in jobs[name]["steps"])


def test_both_native_architectures_are_smoke_tested_and_caches_reach_publisher():
    image = WORKFLOW["jobs"]["image"]
    assert image["strategy"]["matrix"]["include"] == [
        {"arch": "amd64", "runner": "ubuntu-24.04"},
        {"arch": "arm64", "runner": "ubuntu-24.04-arm"},
    ]
    assert image["runs-on"] == "${{ matrix.runner }}"
    build = next(s["with"] for s in image["steps"] if "build-push-action" in s.get("uses", ""))
    assert build["platforms"] == "linux/${{ matrix.arch }}"
    assert build["load"] is True
    assert any("docker run" in s.get("run", "") for s in image["steps"])
    publish = next(s["with"] for s in WORKFLOW["jobs"]["publish-image"]["steps"]
                   if "build-push-action" in s.get("uses", ""))
    for arch in ("amd64", "arm64"):
        assert build["cache-to"].split(",mode=")[0].replace("${{ matrix.arch }}", arch) in publish["cache-from"]
    assert publish["cache-to"] == "type=inline"
    assert "type=registry,ref=ghcr.io/${{ github.repository }}:latest" in build["cache-from"]


def test_current_package_version_has_release_notes():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/changelog_section.py"),
                             f"v{version}"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
