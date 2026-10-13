"""Read-only copies of each repo's default branch, in the private data folder
(checkouts/<owner>__<name>/), for the audit, profiles, reachability and code
search. Downloaded as a tarball through the API: no git, no credentials on
disk, nothing executed. Replaced only when the branch moves."""
from __future__ import annotations

import io
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath

from .security import SKIP_DIRS

MAX_FILE = 2_000_000
TEXT_EXTS = re.compile(r"\.(py|js|jsx|ts|tsx|mjs|cjs|go|rb|php|java|kt|rs|cs|swift|scala|vue|svelte|sql|sh|"
                       r"yaml|yml|toml|json|md|txt|cfg|ini|html|css|tf)$|(^|/)(Dockerfile|Makefile|Gemfile)$")


def slug(repo: str) -> str:
    return repo.replace("/", "__")


def path_for(data_dir: Path, repo: str) -> Path:
    return Path(data_dir) / "checkouts" / slug(repo)


def current_sha(data_dir: Path, repo: str) -> str | None:
    marker = path_for(data_dir, repo) / ".gitzoid-sha"
    return marker.read_text().strip() if marker.is_file() else None


def ensure(gh, repo: str, data_dir: Path) -> tuple[Path, str]:
    """The checkout of the default branch at its current commit. One writer
    at a time per repo (a file lock), and the new copy replaces the old one in
    a single rename."""
    from hubzoid import _file_lock
    import uuid

    root = path_for(data_dir, repo)
    branch = gh.default_branch(repo)
    sha = gh.branch_sha(repo, branch)
    if current_sha(data_dir, repo) == sha:
        return root, sha
    root.parent.mkdir(parents=True, exist_ok=True)
    with open(root.parent / f".{root.name}.lock", "a") as lock:
        _file_lock.lock(lock)
        if current_sha(data_dir, repo) == sha:  # another run fetched it while we waited
            return root, sha
        tag = uuid.uuid4().hex[:8]
        tmp = root.with_name(f"{root.name}.new-{tag}")
        tmp.mkdir(parents=True)
        try:
            if gh.name == "sample":
                shutil.copytree(gh.base_dir, tmp, dirs_exist_ok=True)
            else:
                _extract(gh.tarball(repo, sha), tmp)
            (tmp / ".gitzoid-sha").write_text(sha)
            old = root.with_name(f"{root.name}.old-{tag}")
            if root.exists():
                root.rename(old)
            tmp.rename(root)
            shutil.rmtree(old, ignore_errors=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return root, sha


def _extract(data: bytes, target: Path) -> None:
    """Regular files only, each inside `target`; links and huge files skipped."""
    import json
    excluded = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or m.size > MAX_FILE:
                if m.isfile() or m.issym() or m.islnk():
                    excluded.append({"path": "/".join(PurePosixPath(m.name).parts[1:]), "reason": "oversize or link"})
                continue
            parts = PurePosixPath(m.name).parts[1:]  # drop GitHub's <owner>-<repo>-<sha>/ folder
            if not parts or any(p in ("..", "") or p.startswith("/") for p in parts):
                continue
            dest = target.joinpath(*parts)
            if target.resolve() not in dest.resolve().parents:
                continue
            src = tar.extractfile(m)
            if src is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read())
    if excluded:
        (target / ".gitzoid-excluded.json").write_text(json.dumps(excluded))


def text_files(root: Path, limit: int = 20000):
    root = Path(root)
    n = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or any(p in SKIP_DIRS for p in rel.split("/")[:-1]) or rel.startswith(".gitzoid"):
            continue
        if not TEXT_EXTS.search(rel) or path.stat().st_size > MAX_FILE:
            continue
        yield rel, path
        n += 1
        if n >= limit:
            return


def grep(root: Path, query: str, limit: int = 40) -> list[str]:
    """Lines matching `query`: a regular expression if it compiles, else the
    literal text. Case-insensitive."""
    try:
        pattern = re.compile(query, re.I)
    except re.error:
        pattern = re.compile(re.escape(query), re.I)
    hits = []
    for rel, path in text_files(root):
        if pattern.search(rel):
            hits.append(f"{rel}: (path matches)")
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:160]}")
                if len(hits) >= limit:
                    return hits
    return hits


def read(root: Path, rel: str, start: int = 1, end: int | None = None, max_lines: int = 400) -> str:
    root = Path(root).resolve()
    target = (root / rel).resolve()
    if root not in target.parents or not target.is_file():
        raise FileNotFoundError(rel)
    lines = target.read_text(errors="replace").splitlines()
    start = max(1, start)
    end = min(len(lines), end or start + max_lines - 1, start + max_lines - 1)
    body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
    more = f"\n[... {len(lines) - end} more lines]" if end < len(lines) else ""
    return f"{rel} (lines {start}-{end} of {len(lines)})\n{body}{more}"
