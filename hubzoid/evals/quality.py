"""The evals a hub requires, and whether their latest results still count.

    <hub>/quality/manifest.yaml
        version: 1
        required: [refund-policy, approvals/*]   # case ids, or globs over them
        required_tags: [safety]                  # every case carrying one of these tags

``hubzoid doctor`` shows one row per required case:

    ok        its latest result passed, for the case and hub as they are now
    fail      its latest result failed
    pending   it never ran, or the case or the hub's instructions and tools
              changed since it ran (rerun it)

A result counts only for what it tested: each case result records the digest
of its case file, and each run the digest of the hub's instructions, skills,
knowledge, tools, workflows and settings. A pending case never counts as
passing.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import yaml

MANIFEST = Path("quality") / "manifest.yaml"
_HUB_PARTS = ("AGENTS.md", "agents", "skills", "knowledge", "tools_local", "restricted", "workflows",
              "connectors", "settings.yaml", "hub.settings.yaml")
_SKIP_DIRS = {"__pycache__", ".hubzoid", "output", "logs", ".git"}
_MAX_FILE = 5 * 1024 * 1024
_MAX_FILES = 20000


class QualityError(ValueError):
    """quality/manifest.yaml cannot be used. The message names the problem."""


def hub_digest(hub_dir: Path) -> str:
    """One digest of what a hub's answers depend on: its instructions, skills,
    knowledge, tools, workflows and settings. File order and paths count."""
    from .._fs import resolve_bucket

    hub_dir = Path(hub_dir)
    files: list[Path] = []
    for part in _HUB_PARTS:
        path = hub_dir / part
        if not path.exists() and "." not in part:
            path = resolve_bucket(hub_dir, part) or path
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for f in path.rglob("*"):
                if f.is_file() and not f.is_symlink() and not (set(f.relative_to(hub_dir).parts) & _SKIP_DIRS):
                    files.append(f)
    digest = hashlib.sha256()
    for f in sorted(files, key=lambda p: p.relative_to(hub_dir).as_posix())[:_MAX_FILES]:
        digest.update(f.relative_to(hub_dir).as_posix().encode() + b"\0")
        try:
            size = f.stat().st_size
            digest.update(f.read_bytes() if size <= _MAX_FILE else f"size:{size}".encode())
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def manifest(hub_dir: Path) -> dict | None:
    """The parsed manifest, or None when the hub has none."""
    path = Path(hub_dir) / MANIFEST
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise QualityError(f"{MANIFEST}: cannot be read ({type(exc).__name__})") from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise QualityError(f"{MANIFEST}: starts with version: 1")
    extra = set(data) - {"version", "required", "required_tags"}
    if extra:
        raise QualityError(f"{MANIFEST}: unknown key(s) {', '.join(sorted(extra))}")
    for key in ("required", "required_tags"):
        value = data.get(key) or []
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise QualityError(f"{MANIFEST}: {key} is a list of names")
    return data


def required(hub_dir: Path, cases) -> list:
    """The required cases, in id order. Raises QualityError for a requirement
    that matches no case, so a typo can never read as satisfied."""
    data = manifest(hub_dir)
    if data is None:
        return []
    by_id = {c.name: c for c in cases}
    chosen: dict[str, object] = {}
    for pattern in data.get("required") or []:
        matched = [c for cid, c in by_id.items() if fnmatch(cid, pattern)]
        if not matched:
            raise QualityError(f"{MANIFEST}: no eval case matches {pattern!r}")
        chosen.update({c.name: c for c in matched})
    for tag in data.get("required_tags") or []:
        matched = [c for c in cases if tag in c.tags]
        if not matched:
            raise QualityError(f"{MANIFEST}: no eval case has the tag {tag!r}")
        chosen.update({c.name: c for c in matched})
    return [chosen[k] for k in sorted(chosen)]


@dataclass
class Evidence:
    case_id: str
    status: str          # ok | fail | pending
    summary: str
    fix: str


def evidence(hub_dir: Path, *, history: int = 200) -> list[Evidence]:
    """One verdict per required case, from the latest run that ran it. Raises
    QualityError for a manifest problem."""
    from . import cases as cases_lib
    from . import report

    hub_dir = Path(hub_dir)
    if manifest(hub_dir) is None:
        return []
    cases = cases_lib.discover(hub_dir, strict=False)
    wanted = required(hub_dir, cases)
    now = hub_digest(hub_dir)
    latest: dict[str, tuple[object, object]] = {}
    for _path, suite in reversed(report.load_runs(hub_dir, limit=history)):
        for result in suite.cases:
            latest.setdefault(result.name, (suite, result))
    out = []
    for case in wanted:
        fix = f"hubzoid eval run {hub_dir} --case '{case.name}'"
        found = latest.get(case.name)
        if found is None:
            out.append(Evidence(case.name, "pending", "Never run.", fix))
            continue
        suite, result = found
        when = suite.finished_at or suite.started_at
        if not result.case_digest or result.case_digest != case.digest:
            out.append(Evidence(case.name, "pending", f"The case changed since it last ran ({when}).", fix))
        elif not getattr(suite, "hub_digest", None) or suite.hub_digest != now:
            out.append(Evidence(case.name, "pending",
                                f"The hub's instructions, tools or settings changed since it last ran ({when}).",
                                fix))
        elif result.passed:
            out.append(Evidence(case.name, "ok", f"Passed ({when}).", fix))
        else:
            out.append(Evidence(case.name, "fail", f"Failed ({when}): {result.reason}"[:300],
                                f"hubzoid eval explain {hub_dir} '{case.name}'"))
    return out
