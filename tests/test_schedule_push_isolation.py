"""A task's push in a checkout other hubs share (`schedule_runner.push_head`).

Several hubs can live in one git checkout. Another hub's run leaves the tree
dirty, and a bare `git pull --rebase` refuses to run on a dirty tree, so the
push used to fail. The rebase now runs with `--autostash`: the other hub's
uncommitted changes are set aside and put back, never committed or pushed.
When they conflict with what the remote brought in, the checkout is left as it
was and the run fails with a clear message.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hubzoid import schedule_runner as runner


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.org")
    _git(repo, "config", "user.name", "tester")


@pytest.fixture
def shared(tmp_path):
    """One checkout holding two hubs (`hub-a`, `hub-b`), a bare remote, and a
    second clone standing in for another machine that pushes to the remote."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _identity(work)
    for hub in ("hub-a", "hub-b"):
        (work / hub / "knowledge").mkdir(parents=True)
        (work / hub / "knowledge" / "notes.md").write_text(f"{hub} v1\n")
        (work / hub / "AGENTS.md").write_text(f"{hub}\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "-u", "origin", "HEAD:main")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    _identity(other)
    return work, remote, other


def _upstream_change(other: Path, rel: str, text: str) -> None:
    """Another machine pushes a change, so the task's push must rebase first."""
    (other / rel).write_text(text)
    _git(other, "commit", "-q", "-am", f"upstream: {rel}")
    _git(other, "push", "-q", "origin", "HEAD:main")


def _remote_files(remote: Path, rev: str = "main") -> list[str]:
    return _git(remote, "show", "--name-only", "--format=", rev).split()


def test_push_succeeds_with_uncommitted_changes_from_another_hub(shared):
    work, remote, other = shared
    _upstream_change(other, "hub-a/AGENTS.md", "hub-a, edited upstream\n")
    # Hub B is mid-run: an edited file, a staged file and a new file.
    (work / "hub-b" / "knowledge" / "notes.md").write_text("hub-b in progress\n")
    (work / "hub-b" / "AGENTS.md").write_text("hub-b staged\n")
    _git(work, "add", "hub-b/AGENTS.md")
    (work / "hub-b" / "draft.md").write_text("untracked draft\n")
    # Hub A's task commits its own path and pushes.
    (work / "hub-a" / "knowledge" / "notes.md").write_text("hub-a v2\n")

    sha = runner.commit_paths(work / "hub-a", ["knowledge"], "schedule(refresh): v2", push=True)

    assert sha
    # The remote has the task's commit, on top of the upstream change, and it
    # touches only the task's path.
    assert _git(remote, "log", "-1", "--format=%s", "main") == "schedule(refresh): v2"
    assert _git(remote, "log", "-2", "--format=%s", "main").splitlines()[1] == \
        "upstream: hub-a/AGENTS.md"
    assert _remote_files(remote) == ["hub-a/knowledge/notes.md"]
    # Hub B's work is still in the checkout, uncommitted, and never reached the remote.
    assert (work / "hub-b" / "knowledge" / "notes.md").read_text() == "hub-b in progress\n"
    assert (work / "hub-b" / "AGENTS.md").read_text() == "hub-b staged\n"
    assert (work / "hub-b" / "draft.md").read_text() == "untracked draft\n"
    status = _git(work, "status", "--porcelain")
    assert "hub-b/knowledge/notes.md" in status and "hub-b/AGENTS.md" in status
    assert _git(remote, "show", "main:hub-b/knowledge/notes.md") == "hub-b v1"
    assert _git(work, "stash", "list") == ""
    assert _git(work, "rev-parse", "HEAD") == _git(remote, "rev-parse", "main")


def test_autostash_conflict_fails_cleanly_and_leaves_the_checkout_as_it_was(shared):
    work, remote, other = shared
    # The remote changes the very file hub B is editing.
    _upstream_change(other, "hub-b/knowledge/notes.md", "hub-b, edited upstream\n")
    (work / "hub-b" / "knowledge" / "notes.md").write_text("hub-b in progress\n")
    (work / "hub-b" / "AGENTS.md").write_text("hub-b staged\n")
    _git(work, "add", "hub-b/AGENTS.md")
    (work / "hub-a" / "knowledge" / "notes.md").write_text("hub-a v2\n")
    sha = runner.commit_paths(work / "hub-a", ["knowledge"], "schedule(refresh): v2")
    remote_before = _git(remote, "rev-parse", "main")

    with pytest.raises(RuntimeError) as err:
        runner.push_head(work / "hub-a")

    message = str(err.value)
    assert "uncommitted changes elsewhere in this checkout conflict" in message
    assert "as it was" in message and "NOT pushed" in message
    # Nothing was pushed, and the branch is still at the run's own commit.
    assert _git(remote, "rev-parse", "main") == remote_before
    assert _git(work, "rev-parse", "HEAD") == sha
    # Hub B's changes are back exactly: no conflict markers, staging kept.
    assert (work / "hub-b" / "knowledge" / "notes.md").read_text() == "hub-b in progress\n"
    assert _git(work, "diff", "--cached", "--name-only") == "hub-b/AGENTS.md"
    assert _git(work, "ls-files", "-u") == ""
    assert _git(work, "stash", "list") == ""


def test_a_rebase_conflict_on_the_task_commit_still_fails_cleanly(shared):
    work, remote, other = shared
    _upstream_change(other, "hub-a/knowledge/notes.md", "hub-a, edited upstream\n")
    (work / "hub-b" / "knowledge" / "notes.md").write_text("hub-b in progress\n")
    (work / "hub-a" / "knowledge" / "notes.md").write_text("hub-a v2\n")
    sha = runner.commit_paths(work / "hub-a", ["knowledge"], "schedule(refresh): v2")
    remote_before = _git(remote, "rev-parse", "main")

    with pytest.raises(RuntimeError, match="pull --rebase failed") as err:
        runner.push_head(work / "hub-a")

    # The rebase itself ran (past the other hub's dirty tree) and names the conflict.
    assert "Merge conflict in hub-a/knowledge/notes.md" in str(err.value)

    assert _git(remote, "rev-parse", "main") == remote_before
    assert _git(work, "rev-parse", "HEAD") == sha
    assert (work / "hub-b" / "knowledge" / "notes.md").read_text() == "hub-b in progress\n"
    assert _git(work, "stash", "list") == ""
    assert not (work / ".git" / "rebase-merge").exists()


def test_only_the_current_branch_is_pushed(shared):
    """`push.default=matching` would also push every other branch the remote
    has. The task pushes its own branch and nothing else."""
    work, remote, _ = shared
    _git(work, "push", "-q", "origin", "HEAD:side")
    _git(work, "checkout", "-q", "-b", "side", "--track", "origin/side")
    (work / "hub-b" / "AGENTS.md").write_text("unpushed work on another branch\n")
    _git(work, "commit", "-q", "-am", "side work")
    _git(work, "checkout", "-q", "main")
    _git(work, "config", "push.default", "matching")
    side_before = _git(remote, "rev-parse", "side")

    (work / "hub-a" / "knowledge" / "notes.md").write_text("hub-a v2\n")
    runner.commit_paths(work / "hub-a", ["knowledge"], "schedule(refresh): v2", push=True)

    assert _git(remote, "log", "-1", "--format=%s", "main") == "schedule(refresh): v2"
    assert _git(remote, "rev-parse", "side") == side_before


_RUN_TASK = textwrap.dedent('''
    import json, sys
    from hubzoid.workflows import markdown, runtime
    runtime.init(sys.argv[1])
    runtime.launch()
    try:
        print("RESULT " + json.dumps(markdown.enqueue_task(sys.argv[2], "s1").get_result()))
    except Exception as exc:
        print("FAILED " + str(exc))
    runtime.shutdown()
''')


@pytest.mark.slow
def test_a_scheduled_task_pushes_past_another_hubs_dirty_tree(shared, tmp_path):
    """The same through a real markdown task run on the workflow engine."""
    work, remote, other = shared
    _upstream_change(other, "hub-a/AGENTS.md", "hub-a, edited upstream\n")
    hub = work / "hub-a"
    (hub / "schedule").mkdir()
    (hub / "schedule" / "sync.md").write_text(
        '---\nrun: "echo synced > knowledge/notes.md"\ncommit: ["knowledge"]\npush: true\n'
        'schedule: "0 3 * * *"\n---\n\nx\n')
    (work / ".gitignore").write_text(".hubzoid/\nschedule/\n")
    _git(work, "add", ".gitignore")
    _git(work, "commit", "-q", "-m", "ignore runtime state")
    (work / "hub-b" / "knowledge" / "notes.md").write_text("hub-b in progress\n")
    env = {**os.environ,
           "HUBZOID_OPERATIONAL_DB": f"sqlite:///{tmp_path / 'ops.db'}",
           "HUBZOID_DBOS_DB": f"sqlite:///{tmp_path / 'dbos.db'}"}
    proc = subprocess.run([sys.executable, "-c", _RUN_TASK, str(hub), "sync"],
                          capture_output=True, text=True, timeout=180, env=env)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(("RESULT", "FAILED"))),
                None)
    assert line and line.startswith("RESULT "), proc.stdout[-2000:] + proc.stderr[-2000:]
    out = json.loads(line[7:])
    assert out["result"] == "done" and out["pushed"] is True
    assert _remote_files(remote) == ["hub-a/knowledge/notes.md"]
    assert (work / "hub-b" / "knowledge" / "notes.md").read_text() == "hub-b in progress\n"
