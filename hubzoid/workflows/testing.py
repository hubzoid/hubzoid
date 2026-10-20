# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""Run a workflow in a test, without the engine.

    from hubzoid.workflows.testing import run_local

    out = run_local(program_report, hub_dir=hub, inputs={"program": "Advanced"},
                    as_user="meera@example.org")
    assert out.artifacts[0]["key"] == "program-advanced"

The workflow's own function runs inside a run context like a real run: inputs
validated against its model (`hub.input`), `hub.user`, `hub.trigger`,
`hub.team_run`, `hub.state` and `hub.data_dir` all work. Publishing and email
are captured, never stored or sent. Model and agent calls work only if the
test configures them (`workflows.configure`). Steps run directly, without
checkpoints.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

TEAM_TRIGGERS = ("schedule", "operator", "cli", "webhook", "update")


@dataclass
class LocalRun:
    result: object
    inputs: object
    artifacts: list = field(default_factory=list)
    emails: list = field(default_factory=list)


def run_local(fn, *, hub_dir, inputs: dict | None = None, as_user: str | None = None,
              trigger: str = "tool", event: dict | None = None) -> LocalRun:
    """Run workflow `fn` once, here, and return what it did. `trigger` decides
    the scope: tool or personal_schedule run as `as_user` personally; schedule,
    operator, cli, webhook and update are team runs."""
    from .. import db
    from . import context
    from . import inputs as inputs_lib
    from .events import settings
    from .runtime import load_definitions

    hub_dir = Path(hub_dir)
    name = getattr(fn, "__name__", str(fn))
    wf = next((d for d in load_definitions(hub_dir) if d.name == name), None)
    model = wf.inputs if wf is not None else None
    data = inputs
    if data is None and wf is not None and trigger in TEAM_TRIGGERS:
        data = wf.schedule_input
    limit = wf.max_input_bytes if wf is not None else inputs_lib.MAX_BYTES
    values = inputs_lib.validate(model, data, max_bytes=limit) if (model is not None or data) else None
    scope = "team" if trigger in TEAM_TRIGGERS else "personal"
    user = (as_user or "builder@localhost").strip().lower()
    identity = {"subject": user, "account_id": None, "email": user, "legacy_permissions": [],
                "source": "caller" if scope == "personal" else "run_as"}
    run_id = "local-" + secrets.token_hex(4)
    out = LocalRun(result=None, inputs=values)

    def publish(_hub_dir, hub, _identity, workflow, _run_id, request):
        key = request.get("key")
        record = {"path": request["path"], "title": request.get("title"), "key": key,
                  "audience": request.get("audience"), "digest": request.get("digest"),
                  "scope": (request.get("series") or {}).get("scope")}
        out.artifacts.append(record)
        url = f"local://{hub}/{key or 'artifact'}"
        result = {"id": f"local{len(out.artifacts)}", "url": url, "title": request.get("title"),
                  "filename": Path(request["path"]).name}
        if key:
            result["latest_url"] = url
            if request.get("audience") == "domain":
                result["share_url"] = url + "?company"
        return result

    def email(_hub_dir, _hub, _identity, _workflow, _run_id, request):
        out.emails.append(dict(request))
        return {"status": "previewed", "sent": False, "delivery_id": None,
                "recipient": request.get("email_list") or user, "message": "Captured by run_local."}

    saved = (context._PUBLISH_STEP, context._EMAIL_STEP)
    context._PUBLISH_STEP, context._EMAIL_STEP = publish, email
    try:
        with context.run_scope(hub=hub_dir.name.lower(), workflow=name, hub_dir=hub_dir,
                               engine=db.operational_engine(hub_dir), settings=settings(hub_dir),
                               identity=identity, run_id=run_id, event=event, inputs=values,
                               run={"trigger": trigger, "scope": scope,
                                    "started_by": None if trigger in ("schedule", "webhook") else user}):
            out.result = (wf.fn if wf is not None else fn)()
    finally:
        context._PUBLISH_STEP, context._EMAIL_STEP = saved
    return out
