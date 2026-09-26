"""Internal tools injected ONLY into scheduled-task runs — never into chat.

`schedule_runner` builds these per run with `make(hub_dir, task, emit)`:

  * `run_git`        — read/sync git operations on checkouts inside the hub
                       (pull, fetch, log, diff, show, ...). Mutating verbs
                       (commit, push, checkout, reset, ...) are refused: the
                       agent reads and syncs; Hubzoid does the committing,
                       scoped to the task's declared paths.
  * `write_hub_file` — create/overwrite a file, but only under the task's
                       writable paths (declared `commit:` paths + its own
                       scratch dir). Everything else — .env, secrets,
                       raw_data clones, AGENTS.md — is physically refused.

Two more, only when the task's frontmatter opts in (the file is the deliberate
delivery configuration; the model cannot turn them on):

  * `publish_artifact` (`publish_artifacts: true`) — publish a file the run
                       wrote under its writable paths as a private report
                       owned by the account the run acts as.
  * `send_email`     (`send_email: true`) — email that same account (no other
                       recipient exists), optionally linking its reports. At
                       most MAX_EMAILS per run.

All are plain openai-agents FunctionTools, so the existing
`factory_claude._to_claude_tool` adapter makes them work identically on the
Claude backend. Every invocation is appended to the run's JSONL log via
`emit` and mirrored to the python logger.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from agents import function_tool

from .._fs import resolve_bucket

log = logging.getLogger("hubzoid.schedule")

_GIT_TIMEOUT = 300          # seconds per git invocation
_MAX_TOOL_OUTPUT = 60_000   # chars returned to the model per call

# Read-or-sync git verbs the agent may run. Anything else (commit, push,
# checkout, reset, clean, rm, config, remote, ...) is refused.
_GIT_ALLOWED = {
    "pull", "fetch", "log", "diff", "show", "status",
    "rev-parse", "ls-files", "branch", "shortlog", "describe", "remote",
}
# Flags that smuggle writes or command execution through allowed verbs
# (e.g. `log --output=<file>`, `fetch --upload-pack=<cmd>`, `-c core.*=!cmd`).
_GIT_BANNED_PREFIXES = (
    "--output", "--upload-pack", "--receive-pack", "--exec",
    "-c", "--config", "-C", "--git-dir", "--work-tree", "-o",
)


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TOOL_OUTPUT:
        return text
    return (
        text[:_MAX_TOOL_OUTPUT]
        + f"\n... [truncated: {len(text) - _MAX_TOOL_OUTPUT} more chars — "
        "narrow the query (e.g. one file / one commit) to see the rest]"
    )


def make(hub_dir: Path, task, emit: Callable[..., None]) -> list:
    """Build the schedule-run toolset. `task` is a `ScheduledTask`; `emit`
    appends an event dict to the run's JSONL log."""
    hub = Path(hub_dir).resolve()
    writable = [str(p) for p in task.writable_paths()]

    # The hub's `schedule/` policy dir is NEVER agent-writable, even if a task
    # declares `write`/`commit` roots that would otherwise cover it (e.g.
    # commit: ["."]). Otherwise an LLM task could drop a `schedule/evil.md`
    # with a `run:` shell command that the scheduler executes on the next tick
    # — turning a sandboxed file write into arbitrary code execution. The
    # task's own scratch dir lives under `.hubzoid/schedule/`, which is a
    # different path and stays writable.
    _forbidden_roots = [(hub / "schedule").resolve()]
    _sched_bucket = resolve_bucket(hub, "schedule")
    if _sched_bucket is not None:
        _forbidden_roots.append(_sched_bucket.resolve())

    def _resolve_writable(rel: str) -> Path:
        """Resolve a hub-relative path and require it under a writable root."""
        rel = str(rel).strip().lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise PermissionError(f"path must be hub-relative without '..': {rel!r}")
        # Never let a task write into a git internals dir (hooks, config), even
        # under a broad `write`/`commit` grant — a `.git/hooks/*` write plus the
        # post-run `git commit` is a code-exec vector. Parity with the schedule/
        # guard's threat model.
        if ".git" in Path(rel).parts:
            raise PermissionError(f"{rel!r} is under a .git/ directory, which is never writable.")
        target = (hub / rel).resolve()
        for root in _forbidden_roots:
            if target == root or root in target.parents:
                raise PermissionError(
                    f"{rel!r} is under the hub's schedule/ policy folder, which "
                    "is never writable by a task (it defines the schedule itself)."
                )
        for root in writable:
            root_abs = (hub / root).resolve()
            if target == root_abs or root_abs in target.parents:
                return target
        raise PermissionError(
            f"{rel!r} is outside this task's writable paths: {', '.join(writable)}"
        )

    @function_tool
    def run_git(repo: str, args: str) -> str:
        """Run a read/sync git command inside a repo checkout in this hub.

        Allowed verbs: pull, fetch, log, diff, show, status, rev-parse,
        ls-files, branch, shortlog, describe, remote. Mutating verbs
        (commit, push, checkout, reset, ...) are refused — Hubzoid commits
        the declared paths itself after the run.

        Args:
            repo: hub-relative path of the checkout, e.g. "raw_data/core".
                Use "." for the hub itself.
            args: the git arguments, e.g. "log --oneline -20" or
                "show --stat abc123" or "diff a1b2c3..d4e5f6 -- src/".

        Returns:
            Combined stdout/stderr (truncated if huge), prefixed with the
            exit code on failure.
        """
        rel = str(repo).strip().lstrip("/") or "."
        if ".." in Path(rel).parts:
            return f"[run_git refused: repo path {rel!r} escapes the hub]"
        repo_dir = (hub / rel).resolve()
        if repo_dir != hub and hub not in repo_dir.parents:
            return f"[run_git refused: repo path {rel!r} escapes the hub]"
        if not (repo_dir / ".git").exists():
            return f"[run_git: {rel!r} is not a git checkout (no .git)]"
        try:
            argv = shlex.split(args)
        except ValueError as exc:
            return f"[run_git: cannot parse args: {exc}]"
        if not argv:
            return "[run_git: empty git arguments]"
        if argv[0] not in _GIT_ALLOWED:
            return (
                f"[run_git refused: {argv[0]!r} is not allowed. "
                f"Allowed: {', '.join(sorted(_GIT_ALLOWED))}]"
            )
        if argv[0] == "remote" and len(argv) > 1 and argv[1] not in ("-v", "show", "get-url"):
            return "[run_git refused: only read-only `remote` subcommands (-v, show, get-url)]"
        for tok in argv[1:]:
            if tok.startswith(_GIT_BANNED_PREFIXES):
                return f"[run_git refused: flag {tok!r} is not allowed]"
        log.info("schedule[%s] run_git %s: git %s", task.name, rel, args)
        try:
            r = subprocess.run(
                ["git", *argv], cwd=str(repo_dir),
                capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            emit(event="tool", tool="run_git", repo=rel, args=args,
                 exit=None, result=f"timed out after {_GIT_TIMEOUT}s")
            return f"[run_git: timed out after {_GIT_TIMEOUT}s]"
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        out = out.strip() or "(no output)"
        if r.returncode != 0:
            out = (
                f"[exit {r.returncode}] GIT COMMAND FAILED — do not treat this "
                f"step as done.\n{out}"
            )
            log.warning("schedule[%s] run_git %s failed (exit %d): git %s",
                        task.name, rel, r.returncode, args)
        # Log the outcome, not just the call — a failed pull that an agent
        # shrugs off must be visible in the run log (learned the hard way).
        emit(event="tool", tool="run_git", repo=rel, args=args,
             exit=r.returncode, result=out[:300])
        return _truncate(out)

    @function_tool
    def write_hub_file(path: str, content: str) -> str:
        """Create or overwrite a file inside this task's writable paths.

        Writable paths for this run are the task's declared `commit:` paths
        plus its scratch dir (for state.json / notes). Anything else in the
        hub is refused. Read the current file first (read_file) when you
        intend to update rather than replace.

        Args:
            path: hub-relative file path, e.g. "knowledge/billing.md" or
                the state file shown in your instructions.
            content: full new file content (overwrites).

        Returns:
            Confirmation with the byte count, or a refusal explaining the
            allowed paths.
        """
        try:
            target = _resolve_writable(path)
        except PermissionError as exc:
            log.warning("schedule[%s] write refused: %s", task.name, exc)
            return f"[write_hub_file refused: {exc}]"
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, str) else str(content)
        target.write_text(data, encoding="utf-8")
        emit(event="tool", tool="write_hub_file", path=str(path), bytes=len(data))
        log.info("schedule[%s] wrote %s (%d bytes)", task.name, path, len(data))
        return f"wrote {path} ({len(data)} bytes)"

    tools = [run_git, write_hub_file]
    return tools + _delivery_tools(hub, task, emit, _resolve_writable)


MAX_EMAILS = 5  # per scheduled run


def _delivery_tools(hub: Path, task, emit: Callable[..., None], resolve_writable) -> list:
    """publish_artifact / send_email for a task that opted in. Both act for the
    account the run acts as (`task.run_identity`), fixed before the run."""
    if not (getattr(task, "publish_artifacts", False) or getattr(task, "send_email", False)):
        return []
    from ..workflows import context as wctx

    ident = task.run_identity or {}
    sent = {"n": 0}
    tools = []

    if task.publish_artifacts:

        @function_tool
        def publish_artifact(path: str, title: str) -> str:
            """Publish a file you wrote in this run as a report for the person
            this task runs for. Only they can open it until they choose to share
            it. Returns the report's id and link (they sign in to open it).

            Args:
                path: hub-relative path of a file under this task's writable
                    paths (for example the scratch folder in your instructions).
                title: a short human title for the report.
            """
            try:
                target = resolve_writable(path)
            except PermissionError as exc:
                return f"[publish_artifact refused: {exc}]"
            from .._fs import agent_read_refusal

            reason = agent_read_refusal(hub, target) if not str(target).startswith(
                str(hub / ".hubzoid")) else None
            if reason or not target.is_file():
                return f"[publish_artifact refused: {path!r} is not a file this task can publish]"
            try:
                out = wctx.publish_now(str(hub), hub.name.lower(), ident, f"md:{task.name}",
                                       task.run_id or "", {"path": str(target), "title": title})
            except Exception as exc:  # noqa: BLE001 — surface as the tool's result
                emit(event="tool", tool="publish_artifact", path=str(path), error=str(exc))
                return f"[publish_artifact failed: {exc}]"
            emit(event="tool", tool="publish_artifact", path=str(path), artifact=out["id"])
            log.info("schedule[%s] published %s as %s", task.name, path, out["id"])
            return f"Published report {out['id']}: {out['url']}"

        tools.append(publish_artifact)

    if task.send_email:

        @function_tool
        def send_email(subject: str, body: str, artifact_ids: list[str] | None = None) -> str:
            """Email the person this task runs for (the only possible recipient)
            with a short message and optional links to reports published in this
            run. Returns what happened; only "Accepted" means it was sent.

            Args:
                subject: one-line subject.
                body: plain-text message.
                artifact_ids: ids returned by publish_artifact, to link.
            """
            if sent["n"] >= MAX_EMAILS:
                return f"[send_email refused: at most {MAX_EMAILS} emails per run]"
            sent["n"] += 1
            result = wctx.email_now(str(hub), hub.name.lower(), ident, f"md:{task.name}",
                                    task.run_id or "", {"subject": subject, "body": body,
                                                        "artifacts": list(artifact_ids or [])})
            emit(event="tool", tool="send_email", status=result["status"],
                 delivery=result.get("delivery_id"))
            return f"[{result['status']}] {result['message']}"

        tools.append(send_email)
    return tools
