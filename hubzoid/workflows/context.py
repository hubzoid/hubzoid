# Hubzoid workflows. MIT licensed like the rest of the repository.
"""The per-run `hub` proxy: what a workflow author writes against.

`hub` reads like a global but is a `contextvars`-backed proxy bound per run, so
concurrent workflow runs never see each other's state. Everything it exposes is
resolved from the active run's context:

    hub.name                     the current hub
    hub.setting("repo")          admin-settable config value  (read)
    hub.secret("github_token")   a secret, from hub env, never logged
    hub.state["k"]               durable memory, dict-like, (hub, workflow, key)
    hub.call_llm(prompt, ...)    a model call, checkpointed as a step
    hub.call_agent(task, ...)    the full agent loop, checkpointed as a step
    hub.user.id / .attrs / .can("perm")   the caller (a workflow's service identity)

Secrets are read INSIDE the run (from env) and never passed as workflow/step
arguments, so DBOS never checkpoints them.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .state import WorkflowState

# Injectable seams so the façade stays model-free and testable. server.py/cli.py
# wire the real hub runtime; tests set fakes. Signature mirrors the POC.
_LLM: Callable[..., Any] | None = None
_AGENT: Callable[..., Any] | None = None
# DBOS-step-wrapped versions of the seams (set by runtime.launch()). When present
# a workflow's call goes through a checkpointed step, so recovery skips a
# completed model call instead of re-invoking it (durability).
_LLM_STEP: Callable[..., Any] | None = None
_AGENT_STEP: Callable[..., Any] | None = None


def configure(*, llm: Callable[..., Any] | None = None,
              agent: Callable[..., Any] | None = None) -> None:
    """Wire the LLM / agent implementations (called once at boot). Kept a seam so
    runtime-neutral construction stays in factory/runtime/server/cli."""
    global _LLM, _AGENT
    if llm is not None:
        _LLM = llm
    if agent is not None:
        _AGENT = agent


@dataclass(frozen=True)
class RunCtx:
    hub: str
    workflow: str
    hub_dir: Path
    engine: Any                       # SQLAlchemy Engine for the one hub DB
    settings: dict = field(default_factory=dict)
    subject: str = ""                 # the Casbin subject for this run (workflow:<name>)


_run: ContextVar[RunCtx | None] = ContextVar("hubzoid_workflow_run", default=None)


def _ctx() -> RunCtx:
    ctx = _run.get()
    if ctx is None:
        raise RuntimeError(
            "hub is only usable inside a running workflow (no active run context)"
        )
    return ctx


class HubUser:
    """The caller inside a workflow — its service identity `workflow:<name>`."""

    def __init__(self, ctx: RunCtx):
        self._ctx = ctx

    @property
    def id(self) -> str:
        return self._ctx.subject or f"workflow:{self._ctx.workflow}"

    @property
    def attrs(self) -> dict:
        # Per-(hub, subject) attributes; workflows carry none by default.
        return {}

    def can(self, permission: str, hub: str | None = None) -> bool:
        """Whether this service identity holds `permission` in `hub` (default:
        the current hub). Consults the one access store."""
        from ..access import store_for  # lazy: avoids import cycle
        gs = store_for(self._ctx.hub_dir)
        return gs.can(self.id, hub or self._ctx.hub, permission)


class Hub:
    """The per-run proxy. One module-level instance (`hub`); state is per-run."""

    @property
    def name(self) -> str:
        return _ctx().hub

    def setting(self, key: str, default=None):
        return _ctx().settings.get(key, default)

    def secret(self, key: str, default=None):
        # From hub env at call time, resolved inside whatever step calls it.
        return os.environ.get(key.upper(), default)

    @property
    def state(self) -> WorkflowState:
        ctx = _ctx()
        return WorkflowState(ctx.engine, ctx.hub, ctx.workflow)

    @property
    def user(self) -> HubUser:
        return HubUser(_ctx())

    def call_llm(self, prompt: str, **kw):
        if _LLM is None:
            raise RuntimeError(
                "hub.call_llm is not configured; call workflows.configure(llm=...) "
                "at boot (server/cli wires the hub runtime)"
            )
        ctx = _ctx()
        if _LLM_STEP is not None:   # checkpointed inside a DBOS workflow
            return _LLM_STEP(prompt, str(ctx.hub_dir), ctx.subject)
        return _LLM(prompt, hub_dir=ctx.hub_dir, subject=ctx.subject, **kw)

    def call_agent(self, task: str, **kw):
        if _AGENT is None:
            raise RuntimeError(
                "hub.call_agent is not configured; call workflows.configure(agent=...) "
                "at boot (server/cli wires the hub runtime)"
            )
        ctx = _ctx()
        if _AGENT_STEP is not None:
            return _AGENT_STEP(task, str(ctx.hub_dir), ctx.subject)
        return _AGENT(task, hub_dir=ctx.hub_dir, subject=ctx.subject, **kw)


hub = Hub()


@contextmanager
def run_scope(*, hub: str, workflow: str, hub_dir, engine,
              settings: dict | None = None, subject: str = "") -> Iterator[None]:
    """Bind the run context for the duration of a workflow run, then restore."""
    ctx = RunCtx(
        hub=hub, workflow=workflow, hub_dir=Path(hub_dir), engine=engine,
        settings=settings or {}, subject=subject or f"workflow:{workflow}",
    )
    token = _run.set(ctx)
    try:
        yield
    finally:
        _run.reset(token)
