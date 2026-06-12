"""Execute one scheduled task: fresh-context rounds until the agent says DONE.

The harness contract (the lesson from the knowledge-refresh hang: completion
must be a machine-checkable signal, never English the runner has to judge):

  * Every round, the agent MUST end its reply with exactly one line
    `STATUS: DONE — <summary>` or `STATUS: CONTINUE — <what remains>`.
    The runner string-matches that line; nothing else terminates a run early.
  * Every round is a **fresh context** (`Runtime.run` is stateless per call),
    so long backlogs are chunked naturally; continuity lives in the task's
    state file (`<hub>/.hubzoid/schedule/<task>/state.json`), which the
    preamble orders the agent to maintain.
  * Hard caps bound everything: `timeout` seconds per round (asyncio-level),
    `max_rounds` rounds per run, `max_turns` agent turns per round (enforced
    by the backend SDK). A capped run ends `incomplete` — never hangs — and
    the next scheduled fire resumes from the state file.

Runs are backend-agnostic: the task talks to the hub's own Runtime
(`runtime.build`), so `MODEL=claude-local` and any OpenAI/LiteLLM model work
identically. The runner injects two internal tools (`run_git`,
`write_hub_file`) that exist only for scheduled runs.

After a DONE run, the runner — not the agent — captures the result:
`git add/commit` scoped to the task's declared `commit:` pathspecs only
(a dirty tree elsewhere is never swept in), then optionally
`pull --rebase` + `push`.

Every step is appended as JSONL to
`<hub>/.hubzoid/schedule/<task>/runs/<ts>.jsonl` for live tailing and
post-mortem, and mirrored to the `hubzoid.schedule` logger.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .scheduling import ScheduledTask, ScheduleState

log = logging.getLogger("hubzoid.schedule")

# Abort a run after this many consecutive rounds that produced an agent-level
# error (model down, auth broken). Burning all max_rounds on a dead backend
# helps nobody.
_MAX_CONSECUTIVE_ERRORS = 3

_STATUS_RE = re.compile(
    r"^\s*STATUS:\s*(DONE|CONTINUE)\b[\s—:\-]*(.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class RunResult:
    task: str
    result: str                     # "done" | "incomplete" | "error"
    rounds: int = 0
    duration_s: float = 0.0
    run_log: Path | None = None
    commit_sha: str | None = None
    summary: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.result == "done"


def parse_status(text: str) -> tuple[str | None, str]:
    """Extract the agent's completion signal: ("done"|"continue"|None, note).

    The LAST matching line wins — the agent may quote the protocol mid-reply;
    its real signal is the final one.
    """
    matches = list(_STATUS_RE.finditer(text or ""))
    if not matches:
        return None, ""
    m = matches[-1]
    return m.group(1).lower(), m.group(2).strip()


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
def build_prompt(task: ScheduledTask, hub_dir: Path, *, round_no: int,
                 carry: str = "") -> str:
    """Harness preamble + the hub author's instructions (the md body)."""
    writable = "\n".join(f"  - {p}/" for p in task.writable_paths())
    state_file = f"{task.scratch_rel}/state.json"
    carry_block = ""
    if carry:
        carry_block = (
            f"\nPrevious round ended with: {carry}\n"
            f"Read {state_file} and resume — do not redo finished work.\n"
        )
    return f"""[Hubzoid scheduled task "{task.name}" — round {round_no}/{task.max_rounds} — {datetime.now().strftime('%Y-%m-%d %H:%M')}]

You are running UNATTENDED as a scheduled background task in the hub at
{hub_dir}. There is no user: never ask questions — decide and act.

Operating rules:
- Persistent state: keep your progress in {state_file}.
  Read it first. Update it with write_hub_file after EVERY unit of work you
  complete — a later round (or next week's run) resumes ONLY from that file.
- You may create/modify files ONLY under these hub paths (write_hub_file
  enforces this):
{writable}
- run_git gives you read/sync git access (pull, fetch, log, diff, show, ...)
  to checkouts inside the hub. Do NOT git-commit or push — after you finish,
  Hubzoid itself commits the declared paths.
- Tool failures are NOT success: a result starting with "[exit", "[refused"
  or "[run_git" means that step FAILED. Never base conclusions on the output
  of a failed command, never record state as if it succeeded, and mention
  unresolved failures in your final STATUS line.
- Never regress your state file: if a freshly computed value looks OLDER or
  emptier than what the state file already records (e.g. a repo seemingly
  behind its recorded commit), keep the recorded value and flag the anomaly
  instead of overwriting it.
- Budget: about {task.timeout // 60} minutes this round. If the remaining work
  doesn't fit, save state and hand off to the next round instead of rushing.

Finish protocol (MANDATORY): end your reply with exactly ONE final line —
  STATUS: DONE — <one-line summary of what changed>
when the task's goal is fully met, or
  STATUS: CONTINUE — <what remains>
when anything is left. Nothing may follow that line.
{carry_block}
# Task instructions

{task.body}
"""


# ---------------------------------------------------------------------------
# JSONL run log
# ---------------------------------------------------------------------------
class RunLog:
    """Append-only JSONL, flushed per event so `tail -f` works mid-run."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")

    def emit(self, **event: Any) -> None:
        event.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        self._fh.write(json.dumps(event, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# Scoped commit + push (generalized from the old knowledge_sync engine)
# ---------------------------------------------------------------------------
def _git(top: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(top), *args],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def repo_toplevel(path: Path) -> Path | None:
    """Work-tree root containing `path` (the hub may be the repo root or a
    subdir of a larger agents repo), or None when not in a repo."""
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, check=False)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def commit_paths(hub_dir: Path, rel_paths: list[str], message: str,
                 *, push: bool = False) -> str | None:
    """Commit ONLY the given hub-relative pathspecs; optionally rebase+push.

    Returns the new commit sha, or None when those paths have no changes.
    A dirty tree elsewhere (raw_data clones, secrets, local edits) is never
    swept in — this is the safety property unattended runs depend on.

    With `push`, integrates the remote first via `pull --rebase` so the push
    fast-forwards. A rebase conflict is aborted cleanly and raised: the
    commit stays local, nothing is pushed, a human resolves.
    """
    hub_dir = Path(hub_dir)
    top = repo_toplevel(hub_dir)
    if top is None:
        raise RuntimeError(
            f"{hub_dir} is not inside a git repository; cannot commit. "
            "Clone/init the hub's repo, or drop `commit:` from the task."
        )
    specs = [
        os.path.relpath(hub_dir / p, top)
        for p in rel_paths
        if (hub_dir / p).exists()
    ]
    if not specs:
        return None
    if not _git(top, "status", "--porcelain", "--", *specs).strip():
        return None
    _git(top, "add", "--", *specs)
    _git(top, "commit", "-m", message, "--", *specs)
    sha = _git(top, "rev-parse", "HEAD")
    if push:
        pr = subprocess.run(["git", "-C", str(top), "pull", "--rebase"],
                            capture_output=True, text=True, check=False)
        if pr.returncode != 0:
            subprocess.run(["git", "-C", str(top), "rebase", "--abort"],
                           capture_output=True, text=True, check=False)
            raise RuntimeError(
                "git pull --rebase failed (conflict?). The commit exists "
                "locally but was NOT pushed; resolve by hand.\n" + pr.stderr.strip()
            )
        ps = subprocess.run(["git", "-C", str(top), "push"],
                            capture_output=True, text=True, check=False)
        if ps.returncode != 0:
            raise RuntimeError(
                "git push failed. The commit exists locally but was NOT "
                "pushed.\n" + ps.stderr.strip()
            )
    return sha


# ---------------------------------------------------------------------------
# The run loop
# ---------------------------------------------------------------------------
def _default_runtime_factory(hub_dir: Path, task: ScheduledTask,
                             emit: Callable[..., None]):
    """Hub Runtime + the schedule-only tools. Backend comes from MODEL in
    .env — claude-local and OpenAI/LiteLLM models behave identically here."""
    from . import runtime as runtime_lib
    from .tools import schedule_tools

    extra = {t.name: t for t in schedule_tools.make(hub_dir, task, emit)}
    return runtime_lib.build(hub_dir, extra_tools=extra, max_turns=task.max_turns)


async def run_task(hub_dir: Path, task: ScheduledTask, *,
                   runtime_factory: Callable = _default_runtime_factory,
                   ) -> RunResult:
    """Run one scheduled task to completion (or its caps). Never raises —
    every failure mode is a `RunResult(result="error")` with the log path."""
    hub_dir = Path(hub_dir).resolve()
    started = time.monotonic()
    started_dt = datetime.now()
    state = ScheduleState(hub_dir)

    scratch = hub_dir / task.scratch_rel
    log_path = scratch / "runs" / f"{started_dt.strftime('%Y%m%dT%H%M%S')}.jsonl"
    rlog = RunLog(log_path)
    result = RunResult(task=task.name, run_log=log_path, result="error")

    rlog.emit(event="run_start", task=task.name, schedule=task.schedule,
              timeout=task.timeout, max_rounds=task.max_rounds,
              max_turns=task.max_turns, writable=task.writable_paths(),
              commit=task.commit, push=task.push)
    log.info("schedule[%s] run start (timeout=%ss, max_rounds=%s) — log: %s",
             task.name, task.timeout, task.max_rounds, log_path)
    state.record_fired(task.name, started_dt, result="running",
                       run_log=str(log_path))

    try:
        rt = runtime_factory(hub_dir, task, rlog.emit)
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the scheduler
        result.error = f"runtime build failed: {type(exc).__name__}: {exc}"
        rlog.emit(event="error", where="runtime_build", error=result.error,
                  traceback=traceback.format_exc())
        log.error("schedule[%s] %s", task.name, result.error)
        state.record_fired(task.name, started_dt, result="error")
        result.duration_s = time.monotonic() - started
        rlog.emit(event="run_end", result="error", rounds=0,
                  duration_s=round(result.duration_s, 1))
        rlog.close()
        return result

    carry = ""
    consecutive_errors = 0
    done = False
    try:
        # Open MCP here and close it in the finally below — same task, so the
        # stdio connection's cancel scope is entered/exited consistently even
        # though each rt.run() executes in a wait_for child task. Guarded with
        # hasattr because runtime_factory is a public extension point.
        if hasattr(rt, "aopen"):
            await rt.aopen()
        for round_no in range(1, task.max_rounds + 1):
            result.rounds = round_no
            prompt = build_prompt(task, hub_dir, round_no=round_no, carry=carry)
            rlog.emit(event="round_start", round=round_no, carry=carry)
            log.info("schedule[%s] round %d/%d%s", task.name, round_no,
                     task.max_rounds, f" (carry: {carry[:80]})" if carry else "")
            t0 = time.monotonic()
            try:
                reply = await asyncio.wait_for(rt.run(prompt), timeout=task.timeout)
            except asyncio.TimeoutError:
                rlog.emit(event="round_timeout", round=round_no,
                          timeout=task.timeout)
                log.warning("schedule[%s] round %d hit the %ss timeout; "
                            "resuming from state next round",
                            task.name, round_no, task.timeout)
                carry = (f"CONTINUE — round {round_no} was cut off by the "
                         f"{task.timeout}s timeout mid-work")
                consecutive_errors = 0
                continue

            dt = round(time.monotonic() - t0, 1)
            status, note = parse_status(reply)
            rlog.emit(event="agent_reply", round=round_no, duration_s=dt,
                      chars=len(reply), text=reply)
            rlog.emit(event="round_end", round=round_no, status=status or "missing",
                      note=note, duration_s=dt)

            if "[agent error:" in reply and status is None:
                consecutive_errors += 1
                log.error("schedule[%s] round %d agent error (%d consecutive)",
                          task.name, round_no, consecutive_errors)
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    result.error = ("backend erroring repeatedly; aborting run "
                                    f"after {consecutive_errors} bad rounds")
                    rlog.emit(event="error", where="agent", error=result.error)
                    break
                carry = "CONTINUE — previous round failed with a backend error"
                continue
            consecutive_errors = 0

            if status == "done":
                result.summary = note
                done = True
                log.info("schedule[%s] DONE after round %d: %s",
                         task.name, round_no, note or "(no summary)")
                break
            carry = f"CONTINUE — {note}" if note else (
                "CONTINUE — (the previous round ended without a STATUS line; "
                "check the state file for where it left off)")
            log.info("schedule[%s] round %d → continue: %s",
                     task.name, round_no, note or "no STATUS line")

        if done:
            result.result = "done"
        elif not result.error:
            result.result = "incomplete"
            log.warning("schedule[%s] incomplete after %d round(s); the next "
                        "scheduled fire resumes from the state file",
                        task.name, result.rounds)

        # Capture: commit (and push) ONLY the declared paths, only on DONE.
        if done and task.commit:
            date = started_dt.strftime("%Y-%m-%d")
            summary = re.sub(r"\s+", " ", result.summary).strip()[:100]
            msg = f"schedule({task.name}): {summary or f'run {date}'}"
            try:
                sha = commit_paths(hub_dir, task.commit, msg, push=task.push)
            except RuntimeError as exc:
                result.result = "error"
                result.error = str(exc)
                rlog.emit(event="error", where="commit", error=str(exc))
                log.error("schedule[%s] commit/push failed: %s", task.name, exc)
            else:
                result.commit_sha = sha
                if sha:
                    rlog.emit(event="commit", sha=sha, paths=task.commit,
                              pushed=task.push, message=msg)
                    log.info("schedule[%s] committed %s%s", task.name, sha[:10],
                             " and pushed" if task.push else "")
                else:
                    rlog.emit(event="commit_skip", reason="no changes in declared paths")
                    log.info("schedule[%s] nothing to commit", task.name)
    except Exception as exc:  # noqa: BLE001 — scheduler must survive anything
        result.result = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        rlog.emit(event="error", where="run_loop", error=result.error,
                  traceback=traceback.format_exc())
        log.exception("schedule[%s] run crashed", task.name)
    finally:
        if hasattr(rt, "aclose"):
            await rt.aclose()

    result.duration_s = time.monotonic() - started
    state.record_fired(task.name, started_dt, result=result.result,
                       run_log=str(log_path))
    rlog.emit(event="run_end", result=result.result, rounds=result.rounds,
              duration_s=round(result.duration_s, 1),
              commit_sha=result.commit_sha, error=result.error)
    rlog.close()
    log.info("schedule[%s] run end: %s (%d round(s), %.0fs)",
             task.name, result.result, result.rounds, result.duration_s)
    return result
