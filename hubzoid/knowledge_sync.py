"""Knowledge refresh — keep `<hub>/knowledge/*.md` in step with source code.

Two halves, deliberately separated:

  * **Deterministic (this module):** pull the source repos, enumerate the
    commits since the last successful refresh, and track a per-commit
    worklist + a per-repo SHA cursor on disk. No LLM here — just git and
    JSON state, so it's exhaustive and resumable.

  * **Agentic (the `/goal` worker, driven by `hubzoid knowledge refresh`):**
    a headless `claude -p` session loads the procedure, reads the pending
    worklist, updates the affected knowledge docs, and marks each commit
    done — kept running by `/goal` until nothing is pending. The CLI loops
    fresh sessions until the worklist is clear, so a single session hitting
    a context limit never drops commits.

The cursor advances only when the whole worklist is done, so a refresh is
all-or-nothing per run and safe to re-run.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

SYNC_DIR = ".knowledge-sync"
_UNIT_SEP = "\x1f"


@dataclass
class Commit:
    repo: str
    sha: str
    subject: str
    done: bool = False


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def head_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def git_pull(repo: Path) -> bool:
    """Fast-forward pull. Returns False on failure (offline, no remote) — a
    refresh can still proceed against whatever is already checked out."""
    r = subprocess.run(
        ["git", "-C", str(repo), "pull", "--ff-only"],
        capture_output=True, text=True, check=False,
    )
    return r.returncode == 0


def list_commits(repo: Path, since: str | None, since_days: int | None = None) -> list[tuple[str, str]]:
    """(sha, subject) for the commits to fold in, oldest first.

    With a cursor (`since`), that's `since..HEAD`. With **no** cursor the first
    refresh would otherwise be the repo's *entire* history; `since_days` bounds
    it to commits from the last N days so a first run is a sane window, not all
    of history. No cursor and no `since_days` => whole history.
    """
    args = ["log", "--no-merges", "--reverse", f"--format=%H{_UNIT_SEP}%s"]
    if since:
        args.append(f"{since}..HEAD")
    else:
        if since_days is not None:
            args.append(f"--since={since_days} days ago")
        args.append("HEAD")
    out = _git(repo, *args)
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(_UNIT_SEP)
        rows.append((sha, subject))
    return rows


def discover_repos(hub_dir: Path) -> dict[str, Path]:
    """Source repos for this hub.

    A `<hub>/.knowledge-sync/repos.json` ({"repos": {name: path}}) wins; paths
    there may be relative to the hub. Otherwise every git checkout directly
    under `<hub>/raw_data/` is a source.
    """
    hub_dir = Path(hub_dir)
    cfg = hub_dir / SYNC_DIR / "repos.json"
    if cfg.is_file():
        data = json.loads(cfg.read_text())
        out: dict[str, Path] = {}
        for name, raw in (data.get("repos") or {}).items():
            p = Path(raw)
            out[name] = p if p.is_absolute() else (hub_dir / p)
        return out
    raw_data = hub_dir / "raw_data"
    repos: dict[str, Path] = {}
    if raw_data.is_dir():
        for child in sorted(raw_data.iterdir()):
            if (child / ".git").exists():
                repos[child.name] = child
    return repos


# ---------------------------------------------------------------------------
# On-disk state
# ---------------------------------------------------------------------------
class SyncState:
    """Per-hub refresh state under `<hub>/.knowledge-sync/`."""

    def __init__(self, hub_dir: Path):
        self.hub_dir = Path(hub_dir)
        self.dir = self.hub_dir / SYNC_DIR
        self.cursor_path = self.dir / "state.json"
        self.worklist_path = self.dir / "worklist.json"

    # --- cursor (last successfully-synced sha per repo) ---
    def cursor(self) -> dict[str, str]:
        if self.cursor_path.is_file():
            return json.loads(self.cursor_path.read_text()).get("repos", {})
        return {}

    def advance_cursor(self, heads: dict[str, str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        cur = {"repos": {}}
        if self.cursor_path.is_file():
            cur = json.loads(self.cursor_path.read_text())
        cur.setdefault("repos", {}).update(heads)
        self.cursor_path.write_text(json.dumps(cur, indent=2))
        if self.worklist_path.exists():
            self.worklist_path.unlink()

    # --- worklist (the current run's commits) ---
    def write_worklist(self, commits: list[Commit], heads: dict[str, str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"heads": heads, "commits": [asdict(c) for c in commits]}
        self.worklist_path.write_text(json.dumps(payload, indent=2))

    def _worklist_raw(self) -> dict:
        if self.worklist_path.is_file():
            return json.loads(self.worklist_path.read_text())
        return {"heads": {}, "commits": []}

    def worklist(self) -> list[Commit]:
        return [Commit(**c) for c in self._worklist_raw().get("commits", [])]

    def worklist_heads(self) -> dict[str, str]:
        return self._worklist_raw().get("heads", {})

    def pending(self) -> list[Commit]:
        return [c for c in self.worklist() if not c.done]

    def is_complete(self) -> bool:
        return not self.pending()

    def mark_done(self, shas: list[str]) -> int:
        targets = set(shas)
        commits = self.worklist()
        heads = self.worklist_heads()
        n = 0
        for c in commits:
            if c.sha in targets and not c.done:
                c.done = True
                n += 1
        self.write_worklist(commits, heads)
        return n


# ---------------------------------------------------------------------------
# Prep: build the worklist from the repos
# ---------------------------------------------------------------------------
def build_worklist(
    hub_dir: Path, *, pull: bool = True, since_days: int | None = None
) -> tuple[list[Commit], dict[str, str]]:
    """Pull each source repo and enumerate commits since the cursor.

    Writes the worklist to disk and returns (commits, heads). Heads are the
    HEAD shas captured now; the cursor advances to them only once every
    commit is marked done (see `SyncState.advance_cursor`).

    `since_days` only applies to repos with **no cursor yet** (a first refresh,
    or a repo newly added to `raw_data/`): it bounds that initial enumeration to
    the last N days instead of the repo's whole history. Repos with a cursor
    always use `cursor..HEAD` regardless.
    """
    state = SyncState(hub_dir)
    repos = discover_repos(hub_dir)
    cursor = state.cursor()
    commits: list[Commit] = []
    heads: dict[str, str] = {}
    for name, path in repos.items():
        if pull:
            git_pull(path)
        heads[name] = head_sha(path)
        for sha, subject in list_commits(path, cursor.get(name), since_days=since_days):
            commits.append(Commit(repo=name, sha=sha, subject=subject))
    state.write_worklist(commits, heads)
    return commits, heads


# ---------------------------------------------------------------------------
# Capture the result: commit (and optionally push) the knowledge/ changes
# ---------------------------------------------------------------------------
def repo_toplevel(path: Path) -> Path | None:
    """The git work-tree root containing `path`, or None if not in a repo.

    The hub may *be* the repo root or a subdir of a larger agents repo; either
    way the commit targets the enclosing repo and scopes to `<hub>/knowledge/`.
    """
    r = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def _committable_paths(hub_dir: Path, top: Path) -> list[str]:
    """Repo-relative paths a refresh commit covers.

    Always `knowledge/`. Plus the cursor (`.knowledge-sync/state.json`) **when
    it's tracked in the repo** (i.e. not gitignored): a refresh advances the
    cursor every run, so committing it alongside the docs keeps the working
    tree clean even when no doc changed. If the cursor is gitignored (the
    runtime-state model), it's left out and only `knowledge/` is committed.
    """
    hub_dir = Path(hub_dir)
    paths = [os.path.relpath(hub_dir / "knowledge", top)]
    state = hub_dir / SYNC_DIR / "state.json"
    if state.exists():
        rel = os.path.relpath(state, top)
        ignored = subprocess.run(
            ["git", "-C", str(top), "check-ignore", "-q", rel],
            capture_output=True, text=True, check=False,
        ).returncode == 0
        if not ignored:
            paths.append(rel)
    return paths


def _paths_dirty(top: Path, paths: list[str]) -> bool:
    return bool(_git(top, "status", "--porcelain", "--", *paths).strip())


def knowledge_dirty(hub_dir: Path) -> bool:
    """True if the refresh result (knowledge/, + tracked cursor) is uncommitted."""
    top = repo_toplevel(hub_dir)
    if top is None:
        return False
    return _paths_dirty(top, _committable_paths(hub_dir, top))


def commit_knowledge(hub_dir: Path, message: str, *, push: bool = False) -> str | None:
    """Commit `<hub>/knowledge/` (and only that path), optionally push.

    Returns the new commit sha, or None if there was nothing to commit. Commits
    `knowledge/` plus the cursor (`.knowledge-sync/state.json`) when that's
    tracked — and only those paths, so a dirty working tree elsewhere (raw_data
    clones, unrelated local edits) is never swept in.

    With `push`, integrates the remote first via `pull --rebase` so the push
    fast-forwards — prod's knowledge commit replays on top of any code commits
    devs pushed meanwhile (different files, so no conflict in practice). A
    rebase conflict is aborted cleanly and raised: the commit stays local,
    nothing is pushed, and a human resolves it.
    """
    hub_dir = Path(hub_dir)
    top = repo_toplevel(hub_dir)
    if top is None:
        raise RuntimeError(
            f"{hub_dir} is not inside a git repository; cannot commit. "
            "Init/clone the agents repo, or drop --commit."
        )
    paths = _committable_paths(hub_dir, top)
    if not _paths_dirty(top, paths):
        return None
    _git(top, "add", "--", *paths)                   # stage modified + new files
    _git(top, "commit", "-m", message, "--", *paths)  # commit ONLY these paths
    sha = head_sha(top)
    if push:
        pr = subprocess.run(
            ["git", "-C", str(top), "pull", "--rebase"],
            capture_output=True, text=True, check=False,
        )
        if pr.returncode != 0:
            subprocess.run(["git", "-C", str(top), "rebase", "--abort"],
                           capture_output=True, text=True, check=False)
            raise RuntimeError(
                "git pull --rebase failed (conflict on a shared file?). Knowledge "
                "was committed locally but NOT pushed; resolve and push by hand.\n"
                + pr.stderr.strip()
            )
        ps = subprocess.run(
            ["git", "-C", str(top), "push"],
            capture_output=True, text=True, check=False,
        )
        if ps.returncode != 0:
            raise RuntimeError(
                "git push failed. Knowledge was committed locally but NOT pushed.\n"
                + ps.stderr.strip()
            )
    return sha
