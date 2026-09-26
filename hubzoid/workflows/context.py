# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""The per-run `hub` proxy: what a workflow author writes against.

`hub` reads like a global but is a `contextvars`-backed proxy bound per run, so
concurrent workflow runs never see each other's state. Everything it exposes is
resolved from the active run's context:

    hub.name                     the current hub
    hub.setting("repo")          admin-settable config value  (read)
    hub.secret("github_token")   a secret, from hub env, never logged
    hub.state["k"]               durable memory, dict-like, per person:
                                 (hub, workflow, run's account, key)
    hub.shared_state["k"]        durable memory shared by everyone who runs it
    hub.run_dir                  a private scratch folder for this run
    hub.publish_artifact(path, title=...)  publish a generated file as a
                                 private artifact owned by the run's account
    hub.send_email(subject, body, artifacts=[...])  email the run's account
    hub.call_llm(prompt, ...)    one tool-free model call (text, JSON or a
                                 Pydantic model), checkpointed as a step
    hub.call_agent(task, ...)    the full agent loop with tools, checkpointed
    hub.call_jev(state, questions) typed decisions with probabilities (Jev via
                                 OpenRouter; experimental), checkpointed
    hub.user.id / .email / .attrs / .can("perm")   the account the run acts as

Secrets are read INSIDE the run (from env) and never passed as workflow/step
arguments, so DBOS never checkpoints them.

Every run acts as an ordinary account (`workflows.identity`): its permissions,
connections, state, reports and email are that person's. The account is
re-checked before each model or agent call, publish and email, so a blocked
account stops at the next one. Personal connections (Open WebUI native MCP) are
used through `hub.call_agent`, whose agent acts as the run's account: it gets
that person's connections, never the author's or an administrator's.
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
_JEV: Callable[..., Any] | None = None     # (spec: dict, hub_dir, subject) -> dict
# DBOS-step-wrapped versions of the seams (set by runtime.launch()). When present
# a workflow's call goes through a checkpointed step, so recovery skips a
# completed model call instead of re-invoking it (durability).
_LLM_STEP: Callable[..., Any] | None = None
_AGENT_STEP: Callable[..., Any] | None = None
_JEV_STEP: Callable[..., Any] | None = None


def configure(*, llm: Callable[..., Any] | None = None,
              agent: Callable[..., Any] | None = None,
              jev: Callable[..., Any] | None = None) -> None:
    """Wire the model / agent / Jev implementations (called once at boot).
    Kept a seam so runtime-specific construction stays in runtime/server/cli."""
    global _LLM, _AGENT, _JEV
    if llm is not None:
        _LLM = llm
    if agent is not None:
        _AGENT = agent
    if jev is not None:
        _JEV = jev


@dataclass(frozen=True)
class RunCtx:
    hub: str
    workflow: str
    hub_dir: Path
    engine: Any                       # SQLAlchemy Engine for the one hub DB
    settings: dict = field(default_factory=dict)
    subject: str = ""                 # the Casbin subject this run acts as
    identity: dict | None = None      # RunIdentity.to_dict() captured at run start
    run_id: str = ""                  # the DBOS workflow id, when there is one


# Checkpointed publish/email steps (set by runtime.launch()), so a recovered run
# returns the recorded artifact or delivery instead of repeating it.
_PUBLISH_STEP: Callable[..., Any] | None = None
_EMAIL_STEP: Callable[..., Any] | None = None

_run: ContextVar[RunCtx | None] = ContextVar("hubzoid_workflow_run", default=None)


def _identity(ctx: RunCtx):
    from .identity import RunIdentity

    if ctx.identity:
        return RunIdentity.from_dict(ctx.identity)
    # A bare run_scope (tests, the legacy path): the old service subject.
    return RunIdentity(ctx.subject or f"workflow:{ctx.workflow}", None, "legacy-service")


def _recheck(ctx: RunCtx, what: str) -> None:
    """Before a protected operation: the run's account must still be usable."""
    from .identity import recheck

    recheck(ctx.hub_dir, ctx.hub, _identity(ctx), what=what)


def publish_now(hub_dir: str, hub: str, identity: dict, workflow: str, run_id: str,
                request: dict, idem_key: str | None = None) -> dict:
    """The publish itself: plain data in and out, so DBOS can checkpoint it."""
    from .. import artifacts
    from .identity import RunIdentity, recheck, require_person

    ident = RunIdentity.from_dict(identity)
    require_person(ident, "Publishing an artifact")
    recheck(Path(hub_dir), hub, ident, what="Publishing")
    return artifacts.publish(
        Path(hub_dir), hub=hub, owner=ident.subject, owner_account=ident.account_id,
        source=Path(request["path"]), title=request.get("title"), workflow=workflow,
        run_id=run_id or None, idem_key=idem_key, audience=request.get("audience") or "owner",
        share_with=request.get("share_with") or ())


def email_now(hub_dir: str, hub: str, identity: dict, workflow: str, run_id: str,
              request: dict, idem_key: str | None = None) -> dict:
    """The send itself: plain data in and out, so DBOS can checkpoint it."""
    from .. import email_delivery
    from .identity import IdentityError, RunIdentity, recheck

    ident = RunIdentity.from_dict(identity)
    try:
        recheck(Path(hub_dir), hub, ident, what="Sending email")
    except IdentityError as exc:
        return dict(status="refused", sent=False, delivery_id=None, recipient=None,
                    message=str(exc))
    return email_delivery.send_to_owner(
        Path(hub_dir), hub=hub, identity=ident, subject=request["subject"],
        body=request.get("body") or "", artifact_ids=request.get("artifacts") or (),
        workflow=workflow, run_id=run_id or None, idem_key=idem_key)


def _ctx() -> RunCtx:
    ctx = _run.get()
    if ctx is None:
        raise RuntimeError(
            "hub is only usable inside a running workflow (no active run context)"
        )
    return ctx


class HubUser:
    """The account the run acts as (see `workflows.identity`)."""

    def __init__(self, ctx: RunCtx):
        self._ctx = ctx

    @property
    def id(self) -> str:
        return _identity(self._ctx).subject

    @property
    def email(self) -> str | None:
        return _identity(self._ctx).email

    @property
    def attrs(self) -> dict:
        """Per-(hub, person) attributes from the access store (e.g. a center),
        for scoping the data a run reads to its person."""
        ident = _identity(self._ctx)
        if not ident.is_person:
            return {}
        from ..access import store_for  # lazy: avoids import cycle

        return store_for(self._ctx.hub_dir).attrs_for(self._ctx.hub, ident.subject)

    def can(self, permission: str, hub: str | None = None) -> bool:
        """Whether this account holds `permission` in `hub` (default: the
        current hub), now. Consults the one access store."""
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
        """Durable memory belonging to the run's account: the same workflow run
        for another person never sees it."""
        ctx = _ctx()
        ident = _identity(ctx)
        return WorkflowState(ctx.engine, ctx.hub, ctx.workflow,
                             owner=ident.subject if ident.is_person else "")

    @property
    def shared_state(self) -> WorkflowState:
        """Durable memory shared by every account that runs this workflow. Keep
        personal data out of it."""
        from .state import SHARED

        ctx = _ctx()
        return WorkflowState(ctx.engine, ctx.hub, ctx.workflow, owner=SHARED)

    @property
    def user(self) -> HubUser:
        return HubUser(_ctx())

    @property
    def run_dir(self) -> Path:
        """A private scratch folder for this run (under `.hubzoid/`, which agent
        file tools and the legacy artifact route cannot reach). Write generated
        files here, then publish them."""
        import re

        ctx = _ctx()
        run = re.sub(r"[^A-Za-z0-9._-]+", "-", ctx.run_id or "local").strip("-.") or "local"
        path = Path(ctx.hub_dir) / ".hubzoid" / "runs" / ctx.workflow / run
        path.mkdir(parents=True, exist_ok=True)
        return path

    def publish_artifact(self, path, *, title: str | None = None,
                         audience: str = "owner", share_with=()) -> dict:
        """Publish an existing file as an artifact owned by the run's account and
        return {"id", "url", "title", "filename", "content_type", "size"}.

        Private to the owner by default. `audience="hub"` (everyone who can use
        this agent) or `audience="people"` with `share_with=["a@x.com",
        {"kind": "group", "principal": "finance"}]` shares it explicitly; both
        need a Console-managed hub. Public links are never made here: the owner
        creates them in the viewer, with permission. Each call stores a new
        artifact; earlier ones are never overwritten. Checkpointed as a step."""
        ctx = _ctx()
        source = Path(path)
        if not source.is_absolute():
            source = Path(ctx.hub_dir) / source
        request = {"path": str(source.resolve()), "title": title, "audience": audience,
                   "share_with": [p if isinstance(p, str) else dict(p) for p in share_with]}
        args = (str(ctx.hub_dir), ctx.hub, _identity(ctx).to_dict(), ctx.workflow,
                ctx.run_id, request)
        if _PUBLISH_STEP is not None:
            return _PUBLISH_STEP(*args)
        return publish_now(*args)

    def send_email(self, subject: str, body: str = "", *, artifacts=(),
                   raise_on_failure: bool = True) -> dict:
        """Email the run's own account (there is no other recipient) with
        optional links to artifacts it published. Returns the delivery result;
        by default raises `EmailError` unless the SMTP server accepted it or it
        was written to the preview outbox. Checkpointed as a step: a recovered
        run never sends an accepted message twice, and an interrupted send is
        reported as ambiguous rather than repeated."""
        from ..email_delivery import EmailError

        ctx = _ctx()
        ids = [a["id"] if isinstance(a, dict) else str(a) for a in artifacts]
        request = {"subject": subject, "body": body, "artifacts": ids}
        args = (str(ctx.hub_dir), ctx.hub, _identity(ctx).to_dict(), ctx.workflow,
                ctx.run_id, request)
        result = _EMAIL_STEP(*args) if _EMAIL_STEP is not None else email_now(*args)
        if raise_on_failure and result["status"] not in ("accepted", "previewed"):
            raise EmailError(result)
        return result

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
        _recheck(ctx, "A model call")
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
        _recheck(ctx, "An agent call")
        if _AGENT_STEP is not None:
            text = _AGENT_STEP(task, str(ctx.hub_dir), ctx.subject)
        else:
            text = _AGENT(task, hub_dir=ctx.hub_dir, subject=ctx.subject)
        if response_model is None:
            return text
        from ..structured import extract_json

        return _validated(extract_json(text), response_model, text)

    def call_jev(self, state, questions: dict, *, model: str = "typesafe/jev-1.13") -> dict:
        """Experimental. Typed decisions from TypeSafe's Jev through OpenRouter.
        Each question is a "noul" (does it hold?), "choice" (which label?) or
        "score" (where on an ordered scale?), with instructions and criteria;
        one request may mix them. Returns {question name: answer}, every answer
        checked against its question. Checkpointed as a step. Needs
        JEV_OPENROUTER_API_KEY; raises `hubzoid.jev.JevError` on any failure."""
        if _JEV is None:
            raise RuntimeError(
                "hub.call_jev is not configured; call workflows.configure(jev=...) at boot"
            )
        spec = {"model": model, "state": state, "questions": questions}
        ctx = _ctx()
        _recheck(ctx, "A Jev call")
        if _JEV_STEP is not None:
            data = _JEV_STEP(spec, str(ctx.hub_dir), ctx.subject)
        else:
            data = _JEV(spec, hub_dir=ctx.hub_dir, subject=ctx.subject)
        return data["answers"]


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
              settings: dict | None = None, subject: str = "",
              identity: dict | None = None, run_id: str = "") -> Iterator[None]:
    """Bind the run context for the duration of a workflow run, then restore.

    With `identity` (a RunIdentity dict) the run acts as that account: it is
    also bound as the request identity (surface `workflow`), so restricted tools,
    personal connections and usage rows all see the same person."""
    if identity:
        subject = identity["subject"]
    ctx = RunCtx(
        hub=hub, workflow=workflow, hub_dir=Path(hub_dir), engine=engine,
        settings=settings or {}, subject=subject or f"workflow:{workflow}",
        identity=identity, run_id=run_id or "",
    )
    token = _run.set(ctx)
    try:
        if identity:
            from ..access import Identity, identity_scope

            with identity_scope(Identity.make(ctx.subject, surface="workflow")):
                yield
        else:
            yield
    finally:
        _run.reset(token)
