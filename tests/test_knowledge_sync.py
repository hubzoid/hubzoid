"""Tests for knowledge refresh (#4).

The deterministic engine (git enumeration + worklist/cursor state) is tested
against a real temp git repo. The CLI `refresh` loop is tested with a stubbed
worker that simulates the claude /goal agent marking commits done — proving
the loop is exhaustive (cursor advances only when nothing is pending) and
guards against a no-progress infinite loop.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubzoid import cli
from hubzoid import knowledge_sync as ks


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _git_repo(path: Path, subjects: list[str]) -> list[str]:
    path.mkdir(parents=True)
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.email", "t@t.dev"], path)
    _run(["git", "config", "user.name", "tester"], path)
    shas = []
    for i, subj in enumerate(subjects):
        (path / f"f{i}.txt").write_text(str(i))
        _run(["git", "add", "-A"], path)
        _run(["git", "commit", "-q", "-m", subj], path)
        shas.append(
            subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
        )
    return shas


def _hub_with_repo(tmp_path, subjects):
    hub = tmp_path / "hub"
    (hub / "knowledge").mkdir(parents=True)
    shas = _git_repo(hub / "raw_data" / "core", subjects)
    return hub, shas


# ---------------------------------------------------------------------------
# git enumeration
# ---------------------------------------------------------------------------
def test_list_commits_since_cursor(tmp_path):
    repo = tmp_path / "r"
    shas = _git_repo(repo, ["one", "two", "three"])
    allc = ks.list_commits(repo, since=None)
    assert [s for s, _ in allc] == shas               # oldest-first, all
    assert [subj for _, subj in allc] == ["one", "two", "three"]
    since_first = ks.list_commits(repo, since=shas[0])
    assert [s for s, _ in since_first] == shas[1:]     # only newer than cursor


def test_discover_repos_finds_raw_data_checkouts(tmp_path):
    hub, _ = _hub_with_repo(tmp_path, ["a"])
    _git_repo(hub / "raw_data" / "api", ["x"])
    (hub / "raw_data" / "notes").mkdir()               # not a git repo -> ignored
    repos = ks.discover_repos(hub)
    assert set(repos) == {"core", "api"}


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def test_worklist_pending_mark_done_and_cursor(tmp_path):
    hub, shas = _hub_with_repo(tmp_path, ["a", "b"])
    commits, heads = ks.build_worklist(hub, pull=False)
    assert len(commits) == 2
    state = ks.SyncState(hub)
    assert len(state.pending()) == 2
    assert not state.is_complete()

    state.mark_done([shas[0]])
    assert [c.sha for c in state.pending()] == [shas[1]]
    state.mark_done([shas[1]])
    assert state.is_complete()

    # Cursor advances to heads and clears the worklist.
    state.advance_cursor(state.worklist_heads())
    assert state.cursor()["core"] == heads["core"]
    assert state.pending() == []
    # A fresh worklist now finds nothing (cursor caught up).
    commits2, _ = ks.build_worklist(hub, pull=False)
    assert commits2 == []


# ---------------------------------------------------------------------------
# CLI refresh loop
# ---------------------------------------------------------------------------
def test_refresh_completes_when_worker_clears_worklist(tmp_path, monkeypatch):
    hub, shas = _hub_with_repo(tmp_path, ["a", "b", "c"])

    def worker_marks_all(h):
        st = ks.SyncState(h)
        st.mark_done([c.sha for c in st.pending()])
        return 0
    monkeypatch.setattr(cli, "_invoke_worker", worker_marks_all)

    res = CliRunner().invoke(cli.app, ["knowledge", "refresh", str(hub), "--no-pull"])
    assert res.exit_code == 0, res.output
    state = ks.SyncState(hub)
    assert state.cursor()["core"] == shas[-1]     # advanced to HEAD
    assert state.pending() == []


def test_refresh_loops_until_done_one_commit_per_round(tmp_path, monkeypatch):
    hub, shas = _hub_with_repo(tmp_path, ["a", "b", "c"])
    rounds = {"n": 0}

    def worker_marks_one(h):
        rounds["n"] += 1
        st = ks.SyncState(h)
        pend = st.pending()
        if pend:
            st.mark_done([pend[0].sha])
        return 0
    monkeypatch.setattr(cli, "_invoke_worker", worker_marks_one)

    res = CliRunner().invoke(cli.app, ["knowledge", "refresh", str(hub), "--no-pull"])
    assert res.exit_code == 0, res.output
    assert rounds["n"] == 3                        # one commit per fresh session
    assert ks.SyncState(hub).is_complete()


def test_refresh_aborts_on_no_progress(tmp_path, monkeypatch):
    hub, _ = _hub_with_repo(tmp_path, ["a", "b"])
    monkeypatch.setattr(cli, "_invoke_worker", lambda h: 0)   # agent does nothing

    res = CliRunner().invoke(cli.app, ["knowledge", "refresh", str(hub), "--no-pull"])
    assert res.exit_code == 1                       # bailed, did not loop forever
    state = ks.SyncState(hub)
    assert len(state.pending()) == 2
    assert state.cursor() == {}                     # cursor NOT advanced


# ---------------------------------------------------------------------------
# Worker prompt + skill guards
# ---------------------------------------------------------------------------
def test_worker_prompt_uses_goal_and_the_cli_verbs():
    p = cli._WORKER_PROMPT
    assert p.startswith("/goal")                    # the persistence mechanism
    assert "hubzoid knowledge pending" in p
    assert "hubzoid knowledge mark-done" in p
    assert "knowledge/" in p


def test_update_knowledge_skill_shipped():
    skill = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "update-knowledge" / "SKILL.md"
    text = skill.read_text()
    assert "name: update-knowledge" in text
    assert "hubzoid knowledge mark-done" in text
