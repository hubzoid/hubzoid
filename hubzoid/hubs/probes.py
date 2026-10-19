"""Doctor checks a hub declares itself, in ``hub.manifest.yaml``:

    version: 1
    doctor:
      - id: repository
        entrypoint: doctor/repository.py:check
        timeout_seconds: 10        # default 10, at most 30
        requires: [FACTORY_REPO]   # settings that must be set before it runs
        required: true             # a failure fails doctor (else it warns)

``check(context)`` returns ``{"status": "ok"|"info"|"warn"|"fail", "summary": "...",
"recovery": "...", "details": ...}``. ``context`` holds ``hub_dir``, the hub's
non-secret ``settings`` and, for secret settings, only whether each is
``available``.

Each check runs in its own process group with a deadline, at most four at
once and 64 KiB of output each. A check that overruns either is killed with
everything it started, so a crash or a hang becomes one failed row and never
stops the others. A crash is reported by its exception class only, because
its message may carry a credential from the environment. Checks are trusted hub code, like the hub's tools: Hubzoid
runs them only when doctor runs, never when a hub is loaded or installed.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .yamlfile import HubFileError, load

MANIFEST = "hub.manifest.yaml"
MAX_OUTPUT = 64 * 1024
MAX_PARALLEL = 4
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ENTRY_RE = re.compile(r"^([A-Za-z0-9_./-]+\.py):([A-Za-z_][A-Za-z0-9_]*)$")
STATUSES = ("ok", "info", "warn", "fail")


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    entrypoint: str
    timeout_seconds: float = Field(default=10, gt=0, le=30)
    requires: list[str] = Field(default_factory=list)
    required: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("an id is lowercase letters, digits, dots, dashes or underscores")
        return value

    @field_validator("entrypoint")
    @classmethod
    def _entry(cls, value: str) -> str:
        if not _ENTRY_RE.match(value) or ".." in value.split(":")[0].split("/") or value.startswith("/"):
            raise ValueError("entrypoint is <relative path>.py:<function>, inside the hub")
        return value


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    doctor: list[Probe] = Field(default_factory=list, max_length=32)


def manifest(hub_dir: Path) -> Manifest | None:
    """The hub's manifest, or None without one. Raises HubFileError."""
    path = Path(hub_dir) / MANIFEST
    if not path.is_file():
        return None
    try:
        parsed = Manifest.model_validate(load(path))
    except ValidationError as exc:
        first = exc.errors()[0]
        raise HubFileError(f"{path}: {'.'.join(map(str, first['loc']))}: {first['msg']}") from None
    ids = [p.id for p in parsed.doctor]
    if len(ids) != len(set(ids)):
        raise HubFileError(f"{path}: two doctor checks share an id")
    return parsed


@dataclass
class Result:
    id: str
    status: str
    summary: str
    recovery: str | None = None
    details: object = None


_RUNNER = r"""
import importlib.util, json, sys
path, func, context = sys.argv[1], sys.argv[2], json.loads(sys.stdin.read())
spec = importlib.util.spec_from_file_location("hubzoid_hub_probe", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = getattr(module, func)(context)
print("\x1ehubzoid-probe" + json.dumps(result, default=str))
"""


def _entry_file(hub_dir: Path, probe: Probe) -> Path:
    rel = probe.entrypoint.split(":")[0]
    path = (hub_dir / rel)
    resolved = path.resolve()
    if not resolved.is_relative_to(hub_dir.resolve()) or path.is_symlink():
        raise HubFileError(f"{probe.id}: {rel} is outside the hub or a link")
    if not resolved.is_file():
        raise HubFileError(f"{probe.id}: {rel} does not exist")
    return resolved


def _stop(proc: subprocess.Popen) -> None:
    """Kill the check and everything it started."""
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _crash_class(stderr: str) -> str:
    """The exception's class name from a traceback, never its message (which
    may carry a credential from the environment)."""
    for line in reversed(stderr.strip().splitlines()):
        name = line.split(":", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name or ""):
            return name.rsplit(".", 1)[-1]
    return "an error"


def _run(hub_dir: Path, probe: Probe, context: dict) -> Result:
    try:
        path = _entry_file(hub_dir, probe)
    except HubFileError as exc:
        return Result(probe.id, "fail", str(exc), "Fix the entrypoint in hub.manifest.yaml.")
    func = probe.entrypoint.split(":")[1]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([sys.executable, "-c", _RUNNER, str(path), func], stdin=subprocess.PIPE,
                                stdout=out, stderr=err, cwd=str(hub_dir), start_new_session=(os.name != "nt"))
        try:
            proc.stdin.write(json.dumps(context).encode())
            proc.stdin.close()
        except OSError:
            pass
        deadline = time.monotonic() + probe.timeout_seconds
        verdict = None
        while proc.poll() is None:
            if time.monotonic() > deadline:
                verdict = Result(probe.id, "fail", f"Did not finish in {probe.timeout_seconds:g} seconds.",
                                 "Make the check faster or raise timeout_seconds (at most 30).")
            elif os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_OUTPUT:
                verdict = Result(probe.id, "fail", "Wrote more than 64 KiB of output.",
                                 "Return a short summary and put the rest in details.")
            if verdict is not None:
                _stop(proc)
                return verdict
            time.sleep(0.05)
        if os.name != "nt":  # anything the check left running
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > MAX_OUTPUT:
            return Result(probe.id, "fail", "Wrote more than 64 KiB of output.",
                          "Return a short summary and put the rest in details.")
        out.seek(0)
        err.seek(0)
        stdout = out.read(MAX_OUTPUT).decode("utf-8", "replace")
        stderr = err.read(MAX_OUTPUT).decode("utf-8", "replace")
    marker = "\x1ehubzoid-probe"
    if proc.returncode != 0 or marker not in stdout:
        return Result(probe.id, "fail", f"The check crashed with {_crash_class(stderr)}.",
                      f"Run {probe.entrypoint} by hand to see the error.")
    try:
        data = json.loads(stdout.rsplit(marker, 1)[1])
    except ValueError:
        return Result(probe.id, "fail", "The check returned something that is not JSON.")
    if not isinstance(data, dict) or data.get("status") not in STATUSES or not isinstance(data.get("summary"), str):
        return Result(probe.id, "fail", "The check must return {status: ok|info|warn|fail, summary: text}.")
    return Result(probe.id, data["status"], data["summary"][:500],
                  str(data["recovery"])[:500] if data.get("recovery") else None, data.get("details"))


def run(hub_dir: Path, *, only: list[str] | None = None) -> list[Result]:
    """Run the hub's declared checks. A check whose required settings are not
    set is reported without running. Raises HubFileError for a bad manifest."""
    from . import settings as settingslib

    hub_dir = Path(hub_dir)
    parsed = manifest(hub_dir)
    if parsed is None:
        return []
    if only is not None:
        unknown = sorted(set(only) - {p.id for p in parsed.doctor})
        if unknown:
            raise HubFileError(f"No check named {', '.join(unknown)}. This hub's checks: "
                               + (", ".join(p.id for p in parsed.doctor) or "none"))
    probes = [p for p in parsed.doctor if only is None or p.id in only]
    try:
        resolved = settingslib.resolve(hub_dir)
    except HubFileError:
        resolved = []
    context = {"hub_dir": str(hub_dir),
               "settings": {r.decl.name: r.value for r in resolved if not r.decl.secret and r.value is not None},
               "available": {r.decl.name: bool(r.available) for r in resolved if r.decl.secret}}
    ready = {r.decl.name for r in resolved if r.is_set and not r.problems}
    results: dict[str, Result] = {}
    runnable = []
    for probe in probes:
        missing = [name for name in probe.requires if name not in ready]
        if missing:
            results[probe.id] = Result(probe.id, "fail" if probe.required else "info",
                                       f"Not run: {', '.join(missing)} not set.",
                                       "Set them with hubzoid hub configure, then run doctor again.")
        else:
            runnable.append(probe)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        for probe, result in zip(runnable, pool.map(lambda p: _run(hub_dir, p, context), runnable)):
            if result.status == "fail" and not probe.required:
                result.status = "warn"
            results[probe.id] = result
    return [results[p.id] for p in probes]
