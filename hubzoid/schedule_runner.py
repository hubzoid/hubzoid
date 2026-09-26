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
import shlex
import signal
import subprocess
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import _request_ctx
from .scheduling import ScheduledTask, ScheduleState

log = logging.getLogger("hubzoid.schedule")

# Abort a run after this many consecutive rounds that produced an agent-level
# error (model down, auth broken). Burning all max_rounds on a dead backend
# helps nobody.
_MAX_CONSECUTIVE_ERRORS = 3

# A `run:` script triggered by webhook events finds the files it owns here.
EVENTS_ENV = "HUBZOID_WEBHOOK_EVENTS"
# ...and the account the run acts as here.
RUN_AS_ENV = "HUBZOID_RUN_AS"

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
                 carry: str = "", events: list[str] | None = None) -> str:
    """Harness preamble + the hub author's instructions (the md body).

    `events` are the webhook event files this run claimed (absolute paths)."""
    writable = "\n".join(f"  - {p}/" for p in task.writable_paths())
    state_file = f"{task.scratch_rel}/state.json"
    carry_block = ""
    if carry:
        carry_block = (
            f"\nPrevious round ended with: {carry}\n"
            f"Read {state_file} and resume — do not redo finished work.\n"
        )
    events_rule = ""
    if events:
        events_rule = (
            "- Webhook events: this run handles only these files. Any other file in\n"
            "  the inbox arrived later and is left for the next run:\n"
            + "".join(f"    {p}\n" for p in events)
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
{events_rule}
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
        push_head(hub_dir)
    return sha


def push_head(hub_dir: Path) -> None:
    """Integrate the remote (`pull --rebase`) and push. Safe to repeat: pushing
    an already-pushed branch is a no-op. A rebase conflict is aborted cleanly
    and raised, leaving the commit local for a human to resolve."""
    top = repo_toplevel(Path(hub_dir))
    if top is None:
        raise RuntimeError(f"{hub_dir} is not inside a git repository; cannot push.")
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


def commit_message(task: ScheduledTask, summary: str, started: str) -> str:
    """The commit message for a task run: its DONE summary, else the run date."""
    date = started[:10]
    summary = re.sub(r"\s+", " ", summary or "").strip()[:100]
    return f"schedule({task.name}): {summary or f'run {date}'}"


# ---------------------------------------------------------------------------
# Capture: commit the task's declared paths (shared by LLM + script paths)
# ---------------------------------------------------------------------------
def _capture_commit(hub_dir: Path, task: ScheduledTask, result: "RunResult",
                    rlog: "RunLog", started_dt: datetime) -> None:
    """Commit (and optionally push) ONLY the task's declared `commit:` paths.

    Sets `result.commit_sha` on success, or flips the run to `error` if the
    scoped commit/push fails. A dirty tree elsewhere is never swept in — see
    `commit_paths`. The message is synthesized from the run's summary (the
    agent's DONE note for LLM tasks, or `ran <cmd>` for script tasks).
    """
    msg = commit_message(task, result.summary, started_dt.isoformat())
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


# ---------------------------------------------------------------------------
# Plain-cron script execution (`run:` tasks — no LLM harness)
# ---------------------------------------------------------------------------
# Cap on captured stdout/stderr held in memory. Output is written to temp files
# (so the child never blocks on a full pipe and the bridge never buffers GBs of
# a runaway script's output) and only this much of the TAIL is read back — far
# more than the log needs (it keeps [-4000:]).
_MAX_CAPTURE_BYTES = 64 * 1024


def _tail(fh, cap: int) -> str:
    """Read at most the last `cap` bytes of a file object, decoded leniently."""
    try:
        fh.flush()
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - cap))
        data = fh.read()
    except OSError:
        return ""
    if size > cap:
        # Drop the partial first line so the tail starts on a clean boundary —
        # but ONLY when that first newline is near the window start. For one
        # huge line (a big JSON/base64 blob or minified log with no early
        # newline) stripping to the first newline would discard the ENTIRE
        # capture and leave just the marker — which also blanks the failure
        # diagnostic, since the error tail reads from this same value. In that
        # case keep the raw tail bytes so real output is never lost.
        marker = b"...[output truncated]...\n"
        nl = data.find(b"\n")
        data = marker + (data[nl + 1:] if 0 <= nl <= 1024 else data)
    return data.decode("utf-8", errors="replace")


def _run_command(cmd: list[str], *, shell: bool, cwd: str,
                 timeout: int, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a command in its OWN process group and return (rc, stdout, stderr).

    Output goes to temp files (bounded read-back via `_tail`), NOT in-memory
    pipes — a verbose `run:` job can't OOM the shared bridge process, and the
    child never blocks on a full pipe. On timeout the WHOLE process group is
    killed (`start_new_session=True` + `killpg`), so a line that backgrounds
    work or builds a pipeline cannot orphan grandchildren. Re-raises
    `TimeoutExpired` after killing so the caller reports the timeout.
    """
    import tempfile

    with tempfile.TemporaryFile() as outf, tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(
            cmd[0] if shell else cmd,
            shell=shell, cwd=cwd, env=env,
            stdout=outf, stderr=errf,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.wait(timeout=5)   # reap; don't block forever
            except subprocess.TimeoutExpired:
                pass
            raise
        return proc.returncode, _tail(outf, _MAX_CAPTURE_BYTES), _tail(errf, _MAX_CAPTURE_BYTES)


def _execute_script(hub_dir: Path, task: ScheduledTask, result: "RunResult",
                    rlog: "RunLog", events: list[str] | None = None) -> None:
    """Run a `run:` command as a subprocess in the hub dir. Never raises.

    Fills result.result ('done' on exit 0, else 'error'), result.summary
    (used as the commit message), and result.error. This is the plain-cron
    path: a deterministic build/sync script that needs no LLM. The command
    is operator-authored and git-committed (same trust boundary as the OS
    crontab it replaces), so a shell string is run through the shell.
    The webhook event files the run claimed are in HUBZOID_WEBHOOK_EVENTS,
    one absolute path per line. The account the run acts as is in
    HUBZOID_RUN_AS (informational: a script is trusted operator code, not a
    sandbox, and gains no permission from it).
    """
    cmd = task.run or []
    extra = {EVENTS_ENV: "\n".join(events)} if events else {}
    if (task.run_identity or {}).get("subject"):
        extra[RUN_AS_ENV] = task.run_identity["subject"]
    env = {**os.environ, **extra} if extra else None
    display = cmd[0] if task.run_shell else " ".join(shlex.quote(c) for c in cmd)
    result.rounds = 1
    rlog.emit(event="script_start", command=display, shell=task.run_shell,
              timeout=task.timeout)
    log.info("schedule[%s] run: %s", task.name, display)
    try:
        rc, out, err = _run_command(cmd, shell=task.run_shell,
                                    cwd=str(hub_dir), timeout=task.timeout, env=env)
    except subprocess.TimeoutExpired:
        result.result = "error"
        result.error = f"script timed out after {task.timeout}s"
        rlog.emit(event="script_timeout", timeout=task.timeout)
        log.warning("schedule[%s] script timed out after %ss", task.name, task.timeout)
        return
    except (OSError, ValueError) as exc:
        result.result = "error"
        result.error = f"script failed to start: {type(exc).__name__}: {exc}"
        rlog.emit(event="error", where="script_start", error=result.error)
        log.error("schedule[%s] %s", task.name, result.error)
        return

    stdout = (out or "").strip()
    stderr = (err or "").strip()
    rlog.emit(event="script_end", exit=rc,
              stdout=stdout[-4000:], stderr=stderr[-4000:])
    if rc == 0:
        result.result = "done"
        result.summary = f"ran `{display}`"
        log.info("schedule[%s] script done (exit 0)", task.name)
    else:
        tail = stderr[-500:] or stdout[-500:] or "(no output)"
        result.result = "error"
        result.error = f"exit {rc}: {tail}"
        log.warning("schedule[%s] script failed (exit %d)", task.name, rc)


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
    return runtime_lib.build(hub_dir, extra_tools=extra, max_turns=task.max_turns,
                             model=task.model)


def service_subject(task_name: str) -> str:
    """The legacy service identity of a markdown task, `workflow:md:<task>`.
    Runs now act as an ordinary account (`workflows.identity`); this subject is
    used only on a legacy hub with no account configured, and for reporting the
    grants it held before."""
    return f"workflow:md:{task_name}"


def _record_round_usage(hub_dir: Path, task: ScheduledTask, status: str, t0: float) -> None:
    from . import _request_ctx, usage as usage_lib
    from .access import current_identity

    raw = _request_ctx.drain_usage()
    usage_lib.record(
        hub_dir, hub=hub_dir.name, surface="workflow", kind="agent",
        subject=current_identity().user or service_subject(task.name),
        model=raw.get("model") or task.model,
        input_tokens=raw.get("input_tokens"), output_tokens=raw.get("output_tokens"),
        cost_usd=raw.get("cost_usd"), status=raw.get("status") or status,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


async def run_task(hub_dir: Path, task: ScheduledTask, *,
                   runtime_factory: Callable = _default_runtime_factory,
                   capture: bool = True,
                   events: list[str] | None = None,
                   identity=None,
                   ) -> RunResult:
    """Run one scheduled task to completion (or its caps). Never raises —
    every failure mode is a `RunResult(result="error")` with the log path.

    The run acts as `identity` (a `RunIdentity`, captured by the DBOS executor
    before the run; resolved here when called directly): the task's `run_as`,
    else HUBZOID_WORKFLOW_USER, else the setup default. Its scratch folder is
    that person's, and each agent round writes a usage row for them.

    `capture=False` skips the commit/push, for callers (the DBOS executor in
    workflows/markdown.py) that run them as their own checkpointed steps.
    `events` are the webhook event files the run claimed: named in every
    round's prompt, or passed to a `run:` script in HUBZOID_WEBHOOK_EVENTS."""
    from .access import Identity, identity_scope
    from .workflows import identity as idlib

    what = f"Scheduled task {task.name!r}"
    if identity is None:
        try:
            identity = idlib.resolve(Path(hub_dir), run_as=task.run_as,
                                     legacy_subject=service_subject(task.name), what=what)
        except idlib.IdentityError as exc:
            log.error("schedule[%s] %s", task.name, exc)
            return RunResult(task=task.name, result="error", error=str(exc))
    task.run_identity = identity.to_dict()
    task.state_rel = idlib.markdown_scratch(Path(hub_dir), task.name, identity)
    with identity_scope(Identity.make(identity.subject, surface="workflow")):
        return await _run_task(hub_dir, task, runtime_factory=runtime_factory, capture=capture,
                               events=events or [])


async def _run_task(hub_dir: Path, task: ScheduledTask, *,
                    runtime_factory: Callable, capture: bool,
                    events: list[str]) -> RunResult:
    hub_dir = Path(hub_dir).resolve()
    started = time.monotonic()
    started_dt = datetime.now()
    state = ScheduleState(hub_dir)

    scratch = hub_dir / task.scratch_rel
    log_path = scratch / "runs" / f"{started_dt.strftime('%Y%m%dT%H%M%S')}.jsonl"
    rlog = RunLog(log_path)
    result = RunResult(task=task.name, run_log=log_path, result="error")

    ident = task.run_identity or {}
    rlog.emit(event="run_start", task=task.name, schedule=task.schedule,
              timeout=task.timeout, max_rounds=task.max_rounds,
              max_turns=task.max_turns, writable=task.writable_paths(),
              commit=task.commit, push=task.push, events=events,
              run_as=ident.get("subject"), identity_source=ident.get("source"),
              legacy_permissions_not_held=ident.get("legacy_permissions") or [])
    log.info("schedule[%s] run start (timeout=%ss, max_rounds=%s) — log: %s",
             task.name, task.timeout, task.max_rounds, log_path)
    state.record_fired(task.name, started_dt, result="running",
                       run_log=str(log_path))

    # Plain-cron (`run:`) task: run a subprocess instead of the LLM harness,
    # reusing the same lock/state/log/commit machinery. Executed off the event
    # loop so a long script never stalls concurrent chat requests.
    if task.is_script:
        try:
            await asyncio.to_thread(_execute_script, hub_dir, task, result, rlog, events)
            if result.result == "done" and task.commit and capture:
                _capture_commit(hub_dir, task, result, rlog, started_dt)
        except Exception as exc:  # noqa: BLE001 — never raise; run_task's contract
            # e.g. a git pre-commit hook / index lock raises CalledProcessError,
            # which _capture_commit does not catch. Finalize cleanly regardless.
            result.result = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            rlog.emit(event="error", where="script_run", error=result.error,
                      traceback=traceback.format_exc())
            log.exception("schedule[%s] script run crashed", task.name)
        finally:
            result.duration_s = time.monotonic() - started
            state.record_fired(task.name, started_dt, result=result.result,
                               run_log=str(log_path))
            rlog.emit(event="run_end", result=result.result, rounds=result.rounds,
                      duration_s=round(result.duration_s, 1),
                      commit_sha=result.commit_sha, error=result.error)
            rlog.close()
            log.info("schedule[%s] run end: %s (script, %.0fs)",
                     task.name, result.result, result.duration_s)
        return result

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
            if task.run_identity:
                from .workflows.identity import IdentityError, RunIdentity, recheck

                try:  # the account may have been blocked since the last round
                    recheck(hub_dir, hub_dir.name.lower(),
                            RunIdentity.from_dict(task.run_identity),
                            what=f"Scheduled task {task.name!r}")
                except IdentityError as exc:
                    result.error = str(exc)
                    rlog.emit(event="error", where="identity", error=result.error)
                    break
            result.rounds = round_no
            prompt = build_prompt(task, hub_dir, round_no=round_no, carry=carry, events=events)
            rlog.emit(event="round_start", round=round_no, carry=carry)
            log.info("schedule[%s] round %d/%d%s", task.name, round_no,
                     task.max_rounds, f" (carry: {carry[:80]})" if carry else "")
            t0 = time.monotonic()
            round_status = "error"
            try:
                with _request_ctx.chat_scope(None):
                    try:
                        reply = await asyncio.wait_for(rt.run(prompt), timeout=task.timeout)
                        round_status = "error" if "[agent error:" in reply else "ok"
                    finally:
                        _record_round_usage(hub_dir, task, round_status, t0)
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
        if done and task.commit and capture:
            _capture_commit(hub_dir, task, result, rlog, started_dt)
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
