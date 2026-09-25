# Hubzoid workflows. MIT licensed like the rest of the repository.
"""The per-run `hub` proxy: what a workflow author writes against.

`hub` reads like a global but is a `contextvars`-backed proxy bound per run, so
concurrent workflow runs never see each other's state. Everything it exposes is
resolved from the active run's context:

    hub.name                     the current hub
    hub.setting("repo")          admin-settable config value  (read)
    hub.secret("github_token")   a secret, from hub env, never logged
    hub.state["k"]               durable memory, dict-like, (hub, workflow, key)
    hub.call_llm(prompt, ...)    one tool-free model call (text, JSON or a
                                 Pydantic model), checkpointed as a step
    hub.call_agent(task, ...)    the full agent loop with tools, checkpointed
    hub.decide(state, questions) a typed decision with probabilities (Jev via
                                 OpenRouter; experimental), checkpointed
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
_LLM: Callable[..., Any] | None = None     # (spec: dict, hub_dir, subject) -> dict
_AGENT: Callable[..., Any] | None = None   # (task: str, hub_dir, subject) -> str
_DECIDE: Callable[..., Any] | None = None  # (spec: dict, hub_dir, subject) -> dict
# DBOS-step-wrapped versions of the seams (set by runtime.launch()). When present
# a workflow's call goes through a checkpointed step, so recovery skips a
# completed model call instead of re-invoking it (durability).
_LLM_STEP: Callable[..., Any] | None = None
_AGENT_STEP: Callable[..., Any] | None = None
_DECIDE_STEP: Callable[..., Any] | None = None


def configure(*, llm: Callable[..., Any] | None = None,
              agent: Callable[..., Any] | None = None,
              decide: Callable[..., Any] | None = None) -> None:
    """Wire the model / agent / decision implementations (called once at boot).
    Kept a seam so runtime-specific construction stays in runtime/server/cli."""
    global _LLM, _AGENT, _DECIDE
    if llm is not None:
        _LLM = llm
    if agent is not None:
        _AGENT = agent
    if decide is not None:
        _DECIDE = decide


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

    def call_llm(self, prompt: str, *, response_format: str = "text",
                 response_model=None, model: str | None = None,
                 system: str | None = None):
        """One model call with no tools.

        response_format="text" (default) returns a string; "json" returns the
        parsed JSON object as a dict (anything else raises ModelOutputError).
        Passing a Pydantic model class as `response_model` asks for JSON
        matching its schema and returns a validated instance.
        `model` overrides the hub's model (any LiteLLM id, or claude-local).
        Side-effect free, so a failed call may be retried."""
        if _LLM is None:
            raise RuntimeError(
                "hub.call_llm is not configured; call workflows.configure(llm=...) "
                "at boot (server/cli wires the hub runtime)"
            )
        if response_format not in ("text", "json"):
            raise ValueError('response_format must be "text" or "json"')
        schema = None
        if response_model is not None:
            response_format = "json"
            schema = response_model.model_json_schema()
        spec = {"prompt": prompt, "system": system, "model": model,
                "response_format": response_format, "schema": schema}
        ctx = _ctx()
        if _LLM_STEP is not None:   # checkpointed inside a DBOS workflow
            result = _LLM_STEP(spec, str(ctx.hub_dir), ctx.subject)
        else:
            result = _LLM(spec, hub_dir=ctx.hub_dir, subject=ctx.subject)
        if response_format == "text":
            return result["text"]
        return _validated(result["json"], response_model, result["text"])

    def call_agent(self, task: str, *, response_model=None):
        """The hub's full agent, with its tools. Not retried unless the hub sets
        `agent_max_attempts` (a retry could repeat a write). With a Pydantic
        `response_model`, the agent ends with JSON and a validated instance is
        returned; otherwise its reply text."""
        if _AGENT is None:
            raise RuntimeError(
                "hub.call_agent is not configured; call workflows.configure(agent=...) "
                "at boot (server/cli wires the hub runtime)"
            )
        if response_model is not None:
            from ..structured import json_instruction

            task = task + json_instruction(response_model.model_json_schema()).replace(
                "Respond with only", "Finish your reply with")
        ctx = _ctx()
        if _AGENT_STEP is not None:
            text = _AGENT_STEP(task, str(ctx.hub_dir), ctx.subject)
        else:
            text = _AGENT(task, hub_dir=ctx.hub_dir, subject=ctx.subject)
        if response_model is None:
            return text
        from ..structured import extract_json

        return _validated(extract_json(text), response_model, text)

    def decide(self, state, questions: dict, *, model: str = "typesafe/jev-1.13") -> dict:
        """Experimental. A typed decision from TypeSafe's Jev through OpenRouter:
        each question ("noul", "choice" or "score", with instructions and
        criteria) comes back with its answer, probabilities and confidence.
        Returns the `answers` mapping. Needs OPENROUTER_API_KEY."""
        if _DECIDE is None:
            raise RuntimeError(
                "hub.decide is not configured; call workflows.configure(decide=...) at boot"
            )
        for name, q in questions.items():
            if not isinstance(q, dict) or q.get("type") not in ("noul", "choice", "score"):
                raise ValueError(f'question {name!r} needs type "noul", "choice" or "score"')
        spec = {"model": model, "state": state, "questions": questions}
        ctx = _ctx()
        if _DECIDE_STEP is not None:
            data = _DECIDE_STEP(spec, str(ctx.hub_dir), ctx.subject)
        else:
            data = _DECIDE(spec, hub_dir=ctx.hub_dir, subject=ctx.subject)
        return data.get("answers") or {}


def _validated(value, response_model, raw_text: str):
    """Return `value` (a JSON object), or a `response_model` instance validated
    from it. Checked here, after the checkpointed call, so the caller gets
    ModelOutputError with the raw reply."""
    from ..structured import ModelOutputError

    if response_model is None:
        if not isinstance(value, dict):
            raise ModelOutputError("the model's JSON is not an object", raw_text)
        return value
    from pydantic import ValidationError

    try:
        return response_model.model_validate(value)
    except ValidationError as exc:
        raise ModelOutputError(f"the model's JSON did not match {response_model.__name__}: {exc}",
                               raw_text) from exc


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
