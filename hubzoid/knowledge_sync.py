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


def list_commits(repo: Path, since: str | None) -> list[tuple[str, str]]:
    """(sha, subject) for `since..HEAD`, oldest first. Whole history if no cursor."""
    rng = f"{since}..HEAD" if since else "HEAD"
    out = _git(repo, "log", "--no-merges", "--reverse", f"--format=%H{_UNIT_SEP}%s", rng)
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
def build_worklist(hub_dir: Path, *, pull: bool = True) -> tuple[list[Commit], dict[str, str]]:
    """Pull each source repo and enumerate commits since the cursor.

    Writes the worklist to disk and returns (commits, heads). Heads are the
    HEAD shas captured now; the cursor advances to them only once every
    commit is marked done (see `SyncState.advance_cursor`).
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
        for sha, subject in list_commits(path, cursor.get(name)):
            commits.append(Commit(repo=name, sha=sha, subject=subject))
    state.write_worklist(commits, heads)
    return commits, heads
