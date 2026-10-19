"""HTTP routes for workflow access, on each hub's bridge.

  GET  /api/workflow-access              the policy (as YAML and rows), its revision
  GET  /api/workflow-access/resources    this hub's workflows, with my allowed actions
  GET  /api/workflow-access/subjects     people who use this hub
  POST /api/workflow-access/validate     check a document, change nothing
  POST /api/workflow-access/diff         what applying it would add and remove
  PUT  /api/workflow-access              apply it (manage_access), compare-and-set
  POST /api/workflow-access/explain      why someone may or may not do something

Reading the policy and changing it need ``manage_access`` on the workflows.
Anyone signed in may list the workflows with their own actions and ask about
themselves.
"""
from __future__ import annotations

from fastapi import FastAPI, Request

from ..chat.common import db
from ..chat.engine_routes import read_json
from ..control.http import error
from . import access as wf


def _err(exc: wf.AccessError):
    return error(exc.status, exc.code, exc.message, **exc.extra)


def mount(app: FastAPI, ctx) -> None:
    hub_dir = ctx.hub_dir

    async def who(request: Request, *, mutation: bool = False) -> str:
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return (getattr(user, "email", "") or "").lower()

    async def manager(request: Request, *, mutation: bool = False) -> str:
        email = await who(request, mutation=mutation)
        if not (await db(wf.authorize, hub_dir, email, "*", "manage_access")).allowed:
            raise error(403, "permission_denied", "Only someone who manages access to this hub's workflows can "
                                                  "see or change who may use them.")
        return email

    @app.get("/api/workflow-access")
    async def get_policy(request: Request):
        await manager(request)

        def read():
            rows = wf.explicit(hub_dir)
            return {"revision": wf.revision(hub_dir), "yaml": wf.dump(rows),
                    "grants": [g.as_dict() for g in rows], "effective": wf.effective(hub_dir),
                    "actions": list(wf.ACTIONS)}

        return await db(read)

    @app.get("/api/workflow-access/resources")
    async def get_resources(request: Request):
        email = await who(request)

        def read():
            return {"items": [dict(r, allowed_actions=wf.allowed_actions(hub_dir, email, r["key"]))
                              for r in wf.resources(hub_dir)]}

        return await db(read)

    @app.get("/api/workflow-access/subjects")
    async def get_subjects(request: Request):
        await manager(request)
        from .. import access

        def read():
            people = sorted({s for s, _h, p in access.store_for(hub_dir).list_grants(hub_dir.name)
                             if "@" in (s or "") and p == "use_hub"})
            return {"items": [{"subject": p} for p in people] + [{"subject": wf.EVERYONE}]}

        return await db(read)

    @app.post("/api/workflow-access/validate")
    async def validate(request: Request):
        await manager(request)
        body = await read_json(request)
        try:
            rows = await db(wf.validate, hub_dir, str(body.get("yaml") or ""))
        except wf.AccessError as exc:
            return {"valid": False, "error": {"code": exc.code, "message": exc.message, **exc.extra}}
        return {"valid": True, "grants": len(rows)}

    @app.post("/api/workflow-access/diff")
    async def diff(request: Request):
        await manager(request)
        body = await read_json(request)
        try:
            return await db(wf.diff, hub_dir, str(body.get("yaml") or ""))
        except wf.AccessError as exc:
            raise _err(exc)

    @app.put("/api/workflow-access")
    async def put_policy(request: Request):
        email = await manager(request, mutation=True)
        body = await read_json(request)
        try:
            return await db(wf.apply, hub_dir, str(body.get("yaml") or ""),
                            expected_revision=int(body.get("expected_revision", -1)), actor=email)
        except wf.AccessError as exc:
            raise _err(exc)

    @app.post("/api/workflow-access/explain")
    async def explain(request: Request):
        email = await who(request)
        body = await read_json(request)
        try:
            return await db(wf.explain, hub_dir, asker=email, subject=str(body.get("subject") or email),
                            workflow=str(body.get("workflow") or ""), action=str(body.get("action") or ""))
        except wf.AccessError as exc:
            raise _err(exc)
