"""The evals a hub requires, and whether their latest results still count.

    <hub>/quality/manifest.yaml
        version: 1
        required: [refund-policy, approvals/*]   # case ids, or globs over them
        required_tags: [safety]                  # every case carrying one of these tags

``hubzoid doctor`` shows one row per required case:

    ok        its latest result passed, for the case and hub as they are now
    fail      its latest result failed, or the case file cannot be read
    pending   it never ran, ran without its judge, or the case or the hub's
              instructions, tools or configuration changed since (rerun it)

A result counts only for what it tested: each case result records the digest
of its case file, and each run the digest of the hub's instructions, skills,
knowledge, tools, workflows, settings, approval rules, budgets, sandboxes,
write modes and non-secret configuration. A pending case never counts as
passing, and doctor is not ok until it passes.
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
# Read by their exact names: approval rules (hitl.yaml), usage limits and turn
# caps (budgets.yaml), the sandboxes stations run in (sandbox.yaml and
# sandboxes/<id>.yaml) and the write modes that decide which actions are
# offered (work-items.yaml). Each changes what the agent may do or is told.
_BEHAVIOUR_FILES = ("hitl.yaml", "budgets.yaml", "sandbox.yaml", "sandboxes", "work-items.yaml")
_ENV_FILES = (".env", "restricted/.env")      # names and non-secret values only
_SKIP_DIRS = {"__pycache__", ".hubzoid", "output", "logs", ".git"}
_MAX_FILES = 100_000
_CHUNK = 1024 * 1024
INCOMPLETE = "incomplete"


class QualityError(ValueError):
    """quality/manifest.yaml cannot be used. The message names the problem."""


def _configuration(hub_dir: Path, model: str | None) -> bytes:
    """The hub's .env files as they shape answers: every name, and the value of
    each one that is not secret-looking. Secret values never enter the digest;
    rotating a key does not make results stale. Also the model the hub runs
    on: `model` (an eval run's --model), else MODEL from the environment, as
    the runtime reads it, else from the hub's .env."""
    import os

    from dotenv import dotenv_values

    from .calls import is_secret_key

    configured = ""
    if (hub_dir / ".env").is_file():
        try:
            configured = dotenv_values(hub_dir / ".env", interpolate=False).get("MODEL") or ""
        except (OSError, UnicodeDecodeError, ValueError):
            pass
    lines = [f"model={model or os.environ.get('MODEL') or configured}"]
    for name in _ENV_FILES:
        path = hub_dir / name
        if not path.is_file():
            continue
        try:
            values = dotenv_values(path, interpolate=False)
        except (OSError, UnicodeDecodeError, ValueError):
            lines.append(f"{name}: unreadable")
            continue
        for key in sorted(values):
            shown = "" if is_secret_key(key) else (values[key] or "")
            lines.append(f"{name}:{key}={shown}")
    return "\n".join(lines).encode()


def hub_digest(hub_dir: Path, *, model: str | None = None) -> str:
    """One digest of what a hub's answers depend on: its instructions, skills,
    knowledge, tools, workflows, settings, approval rules, budgets, sandboxes,
    write modes and non-secret configuration. File order, paths and every
    byte count. A hub with more than 100,000 such files gets a digest marked
    incomplete, which no result can match."""
    from .._fs import resolve_bucket

    hub_dir = Path(hub_dir)
    files: list[Path] = []
    for part in _HUB_PARTS + _BEHAVIOUR_FILES:
        path = hub_dir / part
        if not path.exists() and part in _HUB_PARTS and "." not in part:
            path = resolve_bucket(hub_dir, part) or path
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for f in path.rglob("*"):
                if f.is_file() and not f.is_symlink() and not (set(f.relative_to(hub_dir).parts) & _SKIP_DIRS):
                    files.append(f)
                    if len(files) > _MAX_FILES:
                        return INCOMPLETE
    digest = hashlib.sha256(_configuration(hub_dir, model) + b"\0")
    for f in sorted(files, key=lambda p: p.relative_to(hub_dir).as_posix()):
        digest.update(f.relative_to(hub_dir).as_posix().encode() + b"\0")
        try:
            with f.open("rb") as handle:
                while chunk := handle.read(_CHUNK):
                    digest.update(chunk)
        except OSError:
            digest.update(b"unreadable")
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def provenance(hub_dir: Path) -> tuple[str | None, str | None, bool | None]:
    """What a results file records about what it tested: the Hubzoid version,
    and, when the hub folder is in a Git work tree, its commit and whether the
    folder had uncommitted changes. Never raises: Git missing, slow or not a
    work tree leaves the Git fields None."""
    import subprocess

    from .. import __version__

    hub_dir = Path(hub_dir)
    commit: str | None = None
    dirty: bool | None = None
    try:
        head = subprocess.run(["git", "-C", str(hub_dir), "rev-parse", "--verify", "HEAD"],
                              capture_output=True, text=True, timeout=5)
        if head.returncode == 0 and head.stdout.strip():
            commit = head.stdout.strip()
            # The hub's own state and output folders are not its content.
            excluded = [f":(exclude){name}" for name in sorted(_SKIP_DIRS - {"__pycache__"})]
            status = subprocess.run(["git", "-C", str(hub_dir), "status", "--porcelain", "--", ".", *excluded],
                                    capture_output=True, text=True, timeout=5)
            dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    return __version__, commit, dirty


def checks_something(case) -> bool:
    """Whether a case asserts anything beyond "the agent did not crash": a
    reply check, a tool check, a judge, or fakes (which fail any unfaked tool
    call)."""
    return bool(case.contains or case.not_contains or case.expect_tools or case.forbid_tools
                or case.expect_tool_args or case.is_judged or case.is_faked)


def gate_cases(hub_dir: Path) -> list:
    """The cases `hubzoid eval run --required` runs: exactly what the manifest
    requires, chosen by `required()` as doctor chooses them. Raises
    QualityError, naming the problem, when the gate cannot be trusted: no
    manifest, nothing required, a requirement that matches no case, an
    unreadable case file, or a required case that is disabled or checks
    nothing."""
    from . import cases as cases_lib

    hub_dir = Path(hub_dir)
    data = manifest(hub_dir)
    if data is None:
        raise QualityError(f"--required needs {MANIFEST}: list the cases CI must pass "
                           "(required: [case ids], required_tags: [tags])")
    if not (data.get("required") or data.get("required_tags")):
        raise QualityError(f"{MANIFEST}: requires no cases, so the gate would check nothing")
    try:
        found = cases_lib.discover(hub_dir)
    except cases_lib.EvalCaseError as exc:
        raise QualityError(str(exc)) from None
    wanted = required(hub_dir, found)
    disabled = [c.name for c in wanted if not c.enabled]
    if disabled:
        raise QualityError(f"required case(s) switched off with enabled: false: {', '.join(disabled)}. "
                           f"Turn them on or take them out of {MANIFEST}")
    empty = [c.name for c in wanted if not checks_something(c)]
    if empty:
        raise QualityError(f"required case(s) check nothing, so they pass whenever the agent answers: "
                           f"{', '.join(empty)}. Add contains, expect_tools or ## Criteria")
    if hub_digest(hub_dir) == INCOMPLETE:
        raise QualityError(f"the hub has more than {_MAX_FILES:,} files, so doctor can never confirm "
                           "its results are current")
    return wanted


def manifest(hub_dir: Path) -> dict | None:
    """The parsed manifest, or None when the hub has none."""
    path = Path(hub_dir) / MANIFEST
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
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


def required(hub_dir: Path, cases, *, unreadable=()) -> list:
    """The required cases, in id order. Raises QualityError for a requirement
    that matches no case, so a typo can never read as satisfied. A requirement
    that only an `unreadable` case file might meet is not raised here: the
    caller reports that file instead."""
    data = manifest(hub_dir)
    if data is None:
        return []
    by_id = {c.name: c for c in cases}
    chosen: dict[str, object] = {}
    for pattern in data.get("required") or []:
        matched = [c for cid, c in by_id.items() if fnmatch(cid, pattern)]
        if not matched and not any(fnmatch(cid, pattern) for cid in unreadable):
            raise QualityError(f"{MANIFEST}: no eval case matches {pattern!r}")
        chosen.update({c.name: c for c in matched})
    for tag in data.get("required_tags") or []:
        matched = [c for c in cases if tag in c.tags]
        if not matched and not unreadable:
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
    problems: list[tuple[str, str]] = []
    cases = cases_lib.discover(hub_dir, strict=False, errors=problems)
    # A case file that cannot be read may be a required one: it fails, and
    # never quietly drops out of the requirement.
    out = [Evidence(rel.removesuffix(".md"), "fail", f"The case cannot be read: {error}"[:300],
                    f"hubzoid eval list {hub_dir}") for rel, error in problems]
    wanted = required(hub_dir, cases, unreadable=[e.case_id for e in out])
    now = hub_digest(hub_dir)
    latest: dict[str, tuple[object, object]] = {}
    for _path, suite in reversed(report.load_runs(hub_dir, limit=history)):
        for result in suite.cases:
            latest.setdefault(result.name, (suite, result))
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
        elif now == INCOMPLETE:
            out.append(Evidence(case.name, "pending",
                                "The hub has too many files to confirm its results are current.", fix))
        elif not getattr(suite, "hub_digest", None) or suite.hub_digest != now:
            out.append(Evidence(case.name, "pending",
                                f"The hub's instructions, tools or configuration changed since it last ran ({when}).",
                                fix))
        elif result.passed and case.is_judged and result.judge is None:
            out.append(Evidence(case.name, "pending",
                                f"It last ran without its judge ({when}), so its criteria were not checked.", fix))
        elif result.passed:
            out.append(Evidence(case.name, "ok", f"Passed ({when}).", fix))
        else:
            out.append(Evidence(case.name, "fail", f"Failed ({when}): {result.reason}"[:300],
                                f"hubzoid eval explain {hub_dir} '{case.name}'"))
    return out
