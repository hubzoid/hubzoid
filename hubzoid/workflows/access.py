"""Who may do what with each workflow (HUB-91).

Ten separate actions, granted per person (or to everyone in the hub with
``everyone``) for one workflow or for all of them (``*``). No action implies
another:

=================  ============================================================
``run``            start the workflow now, with its inputs
``schedule``       set it to repeat for themselves (needs ``run`` too)
``edit``           propose changes to its definition (reviewed as a hub change)
``create``         propose new workflows (``*`` only)
``approve``        review proposed definition changes (never a person's
                   approval of an action; those come from ``hitl.yaml``)
``view_definition``  read its definition
``view_runs``      see its runs: status and timing, not their results
``view_outputs``   open what its runs produced, when the run's audience allows
``control``        pause or resume its schedule, cancel or retry runs
``manage_access``  change these grants for that workflow
=================  ============================================================

Authority lives in the operational store. ``workflows/access.yaml`` is the
same policy as a file to export, review and apply: editing the file alone
grants nothing until ``hubzoid workflow-access apply`` (or the Console) applies
it, and that is audited with a new revision.

Grants made before this existed keep working, read through (never copied):
``workflows_manage`` gives ``run`` and ``control`` on every workflow,
``workflows_view`` gives ``view_definition`` and ``view_runs``, and
``schedule_reports`` gives ``schedule`` on workflows the person may run, and a
report's own grant (``wf_<name>``) gives ``run`` on that report. An
organization administrator keeps ``manage_access``, ``run`` and ``control``.
Nobody gets another person's results from an old grant.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

ACTIONS = ("run", "schedule", "edit", "create", "approve", "view_definition", "view_runs", "view_outputs",
           "control", "manage_access")
FILE = "workflows/access.yaml"
EVERYONE = "everyone"
MAX_ROWS = 10_000

metadata = sa.MetaData()
grants = sa.Table(
    "hz_workflow_access", metadata,
    sa.Column("hub_id", sa.String(255), primary_key=True),
    sa.Column("subject", sa.String(320), primary_key=True),       # an email, or "everyone"
    sa.Column("workflow", sa.String(255), primary_key=True),      # a workflow key, or "*"
    sa.Column("action", sa.String(32), primary_key=True),
    sa.Column("granted_by", sa.String(320), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)
revisions = sa.Table(
    "hz_workflow_access_revisions", metadata,
    sa.Column("hub_id", sa.String(255), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("document_sha256", sa.String(64), nullable=False),
    sa.Column("actor", sa.String(320), nullable=False),
    sa.Column("added_json", sa.Text, nullable=False),
    sa.Column("removed_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)


class AccessError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


@dataclass(frozen=True)
class Grant:
    subject: str
    workflow: str
    action: str

    def as_dict(self) -> dict:
        return {"subject": self.subject, "workflow": self.workflow, "action": self.action}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    sources: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "sources": list(self.sources)}


def _engine(hub_dir: Path):
    from ..db import operational_engine

    return operational_engine(Path(hub_dir))


def _hub(hub_dir: Path) -> str:
    return Path(hub_dir).name.lower()


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


# -- the workflows of a hub ---------------------------------------------------------
def resources(hub_dir: Path) -> list[dict]:
    """Every workflow this hub declares: code workflows and markdown tasks."""
    from .. import scheduling
    from . import observe

    out = [{"key": r["name"], "kind": "code", "source": r.get("source")} for r in observe.definitions(hub_dir)]
    try:
        tasks, _problems = scheduling.load_tasks(Path(hub_dir))
    except Exception:  # noqa: BLE001 - a broken task file is reported elsewhere
        tasks = []
    out += [{"key": f"md:{t.name}", "kind": "markdown", "source": f"schedule/{t.name}.md"} for t in tasks]
    from .builtin import files as builtin_files

    out += [{"key": p.stem, "kind": "builtin", "source": "built in"} for p in builtin_files()]
    return sorted(out, key=lambda r: r["key"])


def canonical(hub_dir: Path, workflow: str) -> str:
    """``weekly_report`` and ``md:check`` as the registry names them; a bare
    markdown task name is accepted when no code workflow has it."""
    keys = {r["key"] for r in resources(hub_dir)}
    if workflow in keys or workflow == "*":
        return workflow
    if f"md:{workflow}" in keys:
        return f"md:{workflow}"
    raise AccessError(422, "unknown_workflow", f"No workflow {workflow!r} in this hub.",
                      known=sorted(keys))


# -- reading ---------------------------------------------------------------------------
def explicit(hub_dir: Path) -> list[Grant]:
    g = grants.c
    with _engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(grants).where(g.hub_id == _hub(hub_dir))
                            .order_by(g.workflow, g.subject, g.action)).fetchall()
    return [Grant(r.subject, r.workflow, r.action) for r in rows]


def revision(hub_dir: Path) -> int:
    r = revisions.c
    with _engine(hub_dir).connect() as conn:
        return int(conn.execute(sa.select(sa.func.max(r.revision)).where(r.hub_id == _hub(hub_dir))).scalar() or 0)


def _legacy(hub_dir: Path, email: str) -> list[tuple[str, str, str]]:
    """(workflow, action, source) the person holds through grants made before
    workflow access existed."""
    from .. import access

    out: list[tuple[str, str, str]] = []
    hub = Path(hub_dir).name
    try:
        gs = access.store_for(Path(hub_dir))
        if gs.is_suspended(email):
            return []
        can = lambda perm: gs.can(email, hub, perm)  # noqa: E731
        from .runtime import tool_permission

        for res in resources(hub_dir):  # "Run for me" grants on each report
            if res["kind"] == "code" and can(tool_permission(res["key"])):
                out.append((res["key"], "run", tool_permission(res["key"])))
        # Repeated requests ("repeat this every Monday") are each person's own,
        # run as them with their own access: everyone who uses the agent may
        # run and schedule them, unless the hub turns them off
        # (HUBZOID_PROMPT_WORKFLOWS=false).
        from .builtin import NAMES as BUILTIN, files as builtin_files

        if can("use_hub") and builtin_files():
            for name in BUILTIN:
                out += [(name, "run", "everyone who uses this agent"),
                        (name, "schedule", "everyone who uses this agent")]
        if can("workflows_manage"):
            out += [("*", "run", "workflows_manage"), ("*", "control", "workflows_manage")]
        if can("workflows_view"):
            out += [("*", "view_definition", "workflows_view"), ("*", "view_runs", "workflows_view")]
        if can("schedule_reports"):
            out += [("*", "schedule", "schedule_reports")]
        if gs.can(email, "*", "manage_access"):
            out += [("*", "manage_access", "organization administrator"), ("*", "run", "organization administrator"),
                    ("*", "control", "organization administrator")]
        elif can("manage_access"):
            out += [("*", "manage_access", "hub access owner")]
    except Exception:  # noqa: BLE001 - unreadable store: nothing (fail closed)
        return []
    return out


def authorize(hub_dir: Path, email: str | None, workflow: str, action: str) -> Decision:
    """The one check for an action on a workflow. Fails closed."""
    if action not in ACTIONS:
        return Decision(False, f"unknown action {action}")
    email = _norm(email)
    if not email:
        return Decision(False, "not signed in")
    from ..access import evaluator

    try:
        # The one access check (account status, credential counter, use_hub)
        # comes first; workflow grants only narrow it.
        base = evaluator.authorize(Path(hub_dir), email, "use_hub", hub=Path(hub_dir).name)
        if not base.allowed:
            return Decision(False, "account suspended" if base.reason in ("account_inactive", "blocked",
                                                                          "credentials_revoked")
                            else "no access to this hub")
    except Exception:  # noqa: BLE001 - fail closed
        return Decision(False, "access check unavailable")
    sources = []
    for gr in explicit(hub_dir):
        if gr.action == action and gr.workflow in (workflow, "*") and gr.subject in (email, EVERYONE):
            sources.append(f"grant: {gr.subject} {gr.workflow} {gr.action}")
    for wf, act, src in _legacy(hub_dir, email):
        if act == action and wf in (workflow, "*"):
            sources.append(f"earlier grant: {src}")
    if action == "schedule" and sources and not authorize(hub_dir, email, workflow, "run").allowed:
        return Decision(False, "schedule needs run too")
    if sources:
        return Decision(True, "granted", tuple(sources))
    return Decision(False, "missing_grant")


def allowed_actions(hub_dir: Path, email: str | None, workflow: str) -> list[str]:
    return [a for a in ACTIONS if authorize(hub_dir, email, workflow, a).allowed]


def effective(hub_dir: Path) -> list[dict]:
    """Every explicit grant and every grant read through from earlier
    permissions, for the Console's table."""
    out = [dict(g.as_dict(), source="workflow access") for g in explicit(hub_dir)]
    from .. import access

    try:
        people = sorted({s for s, _h, _p in access.store_for(Path(hub_dir)).list_grants(Path(hub_dir).name)
                         if "@" in (s or "")})
    except Exception:  # noqa: BLE001
        people = []
    for email in people:
        for wf, act, src in _legacy(hub_dir, email):
            out.append({"subject": email, "workflow": wf, "action": act, "source": src})
    return out


# -- the file --------------------------------------------------------------------------
def parse(text: str) -> list[Grant]:
    """Validate a ``workflows/access.yaml`` document. Raises AccessError naming
    the row and line."""
    from ..control import yamlfile

    try:
        loaded = yamlfile.loads(text)
    except yamlfile.YamlError as exc:
        raise AccessError(422, "invalid_policy", exc.message, path=exc.path, line=exc.line) from exc
    data = loaded.data
    bad = sorted(set(data) - {"version", "default", "grants"})
    if bad:
        raise AccessError(422, "invalid_policy", f"Unknown key {bad[0]!r}.", path=f"/{bad[0]}",
                          line=loaded.line_of(f"/{bad[0]}"))
    if data.get("version") != 1:
        raise AccessError(422, "invalid_policy", "version must be 1.", path="/version", line=loaded.line_of("/version"))
    if data.get("default", "deny") != "deny":
        raise AccessError(422, "invalid_policy", "default can only be deny.", path="/default",
                          line=loaded.line_of("/default"))
    rows = data.get("grants") or []
    if not isinstance(rows, list):
        raise AccessError(422, "invalid_policy", "grants is a list.", path="/grants", line=loaded.line_of("/grants"))
    out: list[Grant] = []
    seen = set()
    for i, row in enumerate(rows):
        where = f"/grants/{i}"
        line = loaded.line_of(where)
        if not isinstance(row, dict) or set(row) - {"subject", "workflow", "actions"}:
            raise AccessError(422, "invalid_policy", "Each grant has subject, workflow and actions.", path=where,
                              line=line)
        subject = _norm(str(row.get("subject") or ""))
        if subject.startswith("user:"):
            subject = subject[5:]
        if subject.startswith("group:"):
            raise AccessError(422, "invalid_policy", "Groups are not available yet. Name people, or everyone.",
                              path=f"{where}/subject", line=line)
        if subject != EVERYONE and "@" not in subject:
            raise AccessError(422, "unknown_subject", f"{subject or '(empty)'} is not an email or everyone.",
                              path=f"{where}/subject", line=line)
        workflow = str(row.get("workflow") or "").strip()
        if not workflow:
            raise AccessError(422, "invalid_policy", "workflow is a workflow name or *.", path=f"{where}/workflow",
                              line=line)
        actions = row.get("actions")
        if not isinstance(actions, list) or not actions or len(actions) > len(ACTIONS):
            raise AccessError(422, "invalid_policy", "actions is a non-empty list.", path=f"{where}/actions",
                              line=line)
        for act in actions:
            if act not in ACTIONS:
                raise AccessError(422, "invalid_policy", f"{act!r} is not an action. Use: {', '.join(ACTIONS)}.",
                                  path=f"{where}/actions", line=loaded.line_of(f"{where}/actions"))
            if act == "create" and workflow != "*":
                raise AccessError(422, "invalid_policy", "create is granted on * only.", path=f"{where}/actions",
                                  line=line)
            key = (subject, workflow, act)
            if key in seen:
                raise AccessError(422, "invalid_policy", f"{subject} {workflow} {act} is listed twice.", path=where,
                                  line=line)
            seen.add(key)
            out.append(Grant(subject, workflow, act))
    if len(out) > MAX_ROWS:
        raise AccessError(422, "invalid_policy", f"At most {MAX_ROWS} grants.")
    return out


def dump(rows: list[Grant]) -> str:
    """The policy as ``workflows/access.yaml``, one entry per person and workflow."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for g in rows:
        grouped.setdefault((g.subject, g.workflow), []).append(g.action)
    lines = ["version: 1", "default: deny", "grants:" if grouped else "grants: []"]
    for (subject, workflow), acts in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        ordered = [a for a in ACTIONS if a in acts]
        lines += [f"  - subject: {subject}", f"    workflow: {json.dumps(workflow) if workflow == '*' else workflow}",
                  f"    actions: [{', '.join(ordered)}]"]
    return "\n".join(lines) + "\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def validate(hub_dir: Path, text: str) -> list[Grant]:
    rows = parse(text)
    for g in rows:
        canonical(hub_dir, g.workflow)
    return [Grant(g.subject, canonical(hub_dir, g.workflow), g.action) for g in rows]


def diff(hub_dir: Path, text: str) -> dict:
    new = set(validate(hub_dir, text))
    old = set(explicit(hub_dir))
    return {"added": [g.as_dict() for g in sorted(new - old, key=lambda g: (g.workflow, g.subject, g.action))],
            "removed": [g.as_dict() for g in sorted(old - new, key=lambda g: (g.workflow, g.subject, g.action))],
            "revision": revision(hub_dir)}


def apply(hub_dir: Path, text: str, *, expected_revision: int, actor: str) -> dict:
    """Replace the hub's workflow grants with the document, if nobody changed
    them since ``expected_revision``. One transaction: grants, revision, audit."""
    if not authorize(hub_dir, actor, "*", "manage_access").allowed:
        raise AccessError(403, "delegation_denied", "Only someone who manages access to this hub's workflows "
                          "can change who may use them.")
    rows = validate(hub_dir, text)
    hub = _hub(hub_dir)
    g, r = grants.c, revisions.c
    with _engine(hub_dir).begin() as conn:
        current = int(conn.execute(sa.select(sa.func.max(r.revision)).where(r.hub_id == hub)).scalar() or 0)
        if int(expected_revision) != current:
            raise AccessError(409, "revision_conflict", "Workflow access changed since you read it. Export it "
                              "again, review the difference and apply.", current_revision=current)
        old = {Grant(x.subject, x.workflow, x.action)
               for x in conn.execute(sa.select(grants).where(g.hub_id == hub)).fetchall()}
        new = set(rows)
        conn.execute(grants.delete().where(g.hub_id == hub))
        now = time.time()
        for gr in sorted(new, key=lambda x: (x.workflow, x.subject, x.action)):
            conn.execute(grants.insert().values(hub_id=hub, subject=gr.subject, workflow=gr.workflow,
                                                action=gr.action, granted_by=_norm(actor), created_at=now))
        added = sorted((x.as_dict() for x in new - old), key=lambda d: tuple(d.values()))
        removed = sorted((x.as_dict() for x in old - new), key=lambda d: tuple(d.values()))
        conn.execute(revisions.insert().values(hub_id=hub, revision=current + 1, document_sha256=sha(text),
                                               actor=_norm(actor), added_json=json.dumps(added),
                                               removed_json=json.dumps(removed), created_at=now))
    try:
        from ..access import audit

        audit.record(Path(hub_dir), user=_norm(actor), surface="console", tool="workflow_access",
                     decision="allow", reason=f"revision {current + 1}: +{len(added)} -{len(removed)}")
    except Exception:  # noqa: BLE001 - the change is committed with its revision row
        pass
    return {"revision": current + 1, "changed": bool(added or removed), "added": added, "removed": removed}


def explain(hub_dir: Path, *, asker: str, subject: str, workflow: str, action: str) -> dict:
    """Why ``subject`` may or may not do ``action`` on ``workflow``. A person may
    ask about themselves; asking about others needs manage_access."""
    subject = _norm(subject)
    if subject != _norm(asker) and not authorize(hub_dir, asker, "*", "manage_access").allowed:
        raise AccessError(403, "permission_denied", "You can only ask about your own access.")
    wf = canonical(hub_dir, workflow)
    d = authorize(hub_dir, subject, wf, action)
    return {"subject": subject, "workflow": wf, "action": action, **d.as_dict(), "revision": revision(hub_dir)}


def guard_run(ft, hub_dir: Path, workflow: str, *, surfaces, action: str = "run"):
    """A workflow's own chat tool ("run it for me"): shown and allowed to the
    people who may ``run`` it, on the surfaces it allows. Same audit as the
    access guard: no audit row, no run."""
    import asyncio

    from ..access import audit
    from ..access.identity import current_identity
    from ..access.policy import is_allowed

    hub_dir = Path(hub_dir)
    surfaces = frozenset(surfaces)
    original = ft.on_invoke_tool

    def decide() -> tuple[bool, str]:
        ident = current_identity()
        ok, reason = is_allowed(ident, f"workflow:{workflow}:{action}", allowed_surfaces=surfaces, can=lambda: True)
        if not ok:
            return False, reason
        d = authorize(hub_dir, ident.user, workflow, action)
        return d.allowed, d.reason

    async def invoke(ctx, input_str):
        ident = current_identity()
        allowed, reason = await asyncio.to_thread(decide)
        recorded = await asyncio.to_thread(audit.record, hub_dir, user=ident.user, surface=ident.surface,
                                           tool=ft.name, decision="allow" if allowed else "deny", reason=reason)
        if not allowed:
            return f"[access denied: you can't {action} {workflow}. An admin can give you access in Workflow access.]"
        if not recorded:
            return f"[access denied: '{ft.name}' could not be recorded in the access log, so it was not run.]"
        return await original(ctx, input_str)

    def enabled(*_a, **_k) -> bool:
        return decide()[0]

    enabled.hubzoid_permission = f"workflow:{workflow}:{action}"
    enabled.hubzoid_key = ("workflow-" + action, str(hub_dir), workflow, surfaces)
    from ..tool_classes import copy_tool

    return copy_tool(ft, on_invoke_tool=invoke, is_enabled=enabled)
