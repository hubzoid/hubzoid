"""SCIM 2.0 provisioning: ``/scim/v2/<provider>/...`` (RFC 7643 and 7644).

The identity provider creates, updates, deactivates and deletes Users here
with a provider-scoped bearer token (``hubzoid auth scim token``, shown once).
Resources are validated by scim2-models; filters (``userName eq "..."``) and
PATCH operations use its parser. Groups are not offered in this release.

A change answers success only after it is committed, so a failed
deprovisioning is retried by the provider rather than silently lost.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import store

log = logging.getLogger("hubzoid.auth.scim")

CORE_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
MEDIA = "application/scim+json"


def _error(status: int, detail: str, scim_type: str | None = None) -> JSONResponse:
    body = {"schemas": [ERROR], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(body, status_code=status, media_type=MEDIA)


def _base(request: Request, provider_id: str) -> str:
    from ... import appmode

    root = appmode.public_url() or str(request.base_url).rstrip("/")
    return f"{root.rstrip('/')}/scim/v2/{provider_id}"


def _primary_email(user) -> str | None:
    emails = list(user.emails or [])
    chosen = next((e for e in emails if getattr(e, "primary", False)), emails[0] if emails else None)
    value = getattr(chosen, "value", None) if chosen else None
    return str(value).strip().lower() if value else None


def _display(user) -> str:
    if user.display_name:
        return str(user.display_name)
    if user.name and (user.name.given_name or user.name.family_name):
        return " ".join(p for p in (user.name.given_name, user.name.family_name) if p)
    return ""


def _view(account: dict, base: str) -> dict:
    resource = dict(account["resource"])
    resource.pop("password", None)
    resource.update({
        "schemas": resource.get("schemas") or [CORE_USER], "id": account["id"],
        "userName": account["user_name"],
        "meta": {"resourceType": "User", "location": f"{base}/Users/{account['id']}",
                 "version": f'W/"{account["version"]}"'},
    })
    # ``active`` is shown when the directory set it, and always once it is off.
    if "active" in account["resource"] or not account["active"]:
        resource["active"] = account["active"]
    if account.get("external_id"):
        resource["externalId"] = account["external_id"]
    return resource


_ALWAYS = {"schemas", "id", "meta"}


def _project(resource: dict, attributes: str | None, excluded: str | None) -> dict:
    """``attributes`` / ``excludedAttributes`` (RFC 7644 3.4.2.5), top level."""
    def names(raw):
        out = set()
        for item in (raw or "").split(","):
            item = item.strip()
            if item.startswith(CORE_USER + ":"):
                item = item[len(CORE_USER) + 1:]
            if item:
                out.add(item.split(".")[0].lower())
        return out

    wanted, dropped = names(attributes), names(excluded)
    out = {}
    for key, value in resource.items():
        low = key.lower()
        if key in _ALWAYS or low == "id":
            out[key] = value
        elif wanted and low not in wanted:
            continue
        elif low in dropped:
            continue
        else:
            out[key] = value
    return out


def _parse_user(body: dict):
    from scim2_models import User

    clean = {k: v for k, v in (body or {}).items() if k not in ("id", "meta", "password")}
    clean.setdefault("schemas", [CORE_USER])
    return User.model_validate(clean), clean


def mount(app: FastAPI, hub_dir: Path, **_ctx) -> None:
    hub_dir = Path(hub_dir)

    def authorized(request: Request, provider_id: str) -> str | None:
        raw = request.headers.get("authorization") or ""
        if not raw.lower().startswith("bearer "):
            return None
        owner = store.token_provider(hub_dir, raw[7:].strip())
        if owner != provider_id:
            return None
        provider = store.get_provider(hub_dir, provider_id)
        return provider_id if provider is not None else None

    async def guard(request: Request, provider_id: str):
        ok = await run_in_threadpool(authorized, request, provider_id)
        if not ok:
            return _error(401, "A valid SCIM token for this provider is required.")
        return None

    @app.get("/scim/v2/{provider_id}/ServiceProviderConfig", include_in_schema=False)
    async def spc(provider_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        return JSONResponse({
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True}, "bulk": {"supported": False, "maxOperations": 0,
                                                   "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False}, "sort": {"supported": False},
            "etag": {"supported": True},
            "authenticationSchemes": [{"type": "oauthbearertoken", "name": "Bearer token",
                                       "description": "A Hubzoid SCIM token for this provider."}],
        }, media_type=MEDIA)

    @app.get("/scim/v2/{provider_id}/ResourceTypes", include_in_schema=False)
    async def resource_types(provider_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        user_type = _user_type(_base(request, provider_id))
        return JSONResponse({"schemas": [LIST], "totalResults": 1, "startIndex": 1,
                             "itemsPerPage": 1, "Resources": [user_type]}, media_type=MEDIA)

    def _user_schema(base: str) -> dict:
        from scim2_models import User

        schema = User.to_schema().model_dump(mode="json", by_alias=True, exclude_none=True)
        schema["meta"] = {"resourceType": "Schema", "location": f"{base}/Schemas/{CORE_USER}"}
        return schema

    def _user_type(base: str) -> dict:
        return {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": "User", "name": "User", "endpoint": "/Users", "schema": CORE_USER,
                "meta": {"resourceType": "ResourceType", "location": f"{base}/ResourceTypes/User"}}

    @app.get("/scim/v2/{provider_id}/Schemas", include_in_schema=False)
    async def schemas(provider_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        return JSONResponse({"schemas": [LIST], "totalResults": 1, "startIndex": 1,
                             "itemsPerPage": 1, "Resources": [_user_schema(_base(request, provider_id))]},
                            media_type=MEDIA)

    @app.get("/scim/v2/{provider_id}/Schemas/{schema_id}", include_in_schema=False)
    async def schema_by_id(provider_id: str, schema_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        if schema_id != CORE_USER:
            return _error(404, "No such schema.")
        return JSONResponse(_user_schema(_base(request, provider_id)), media_type=MEDIA)

    @app.get("/scim/v2/{provider_id}/ResourceTypes/{type_id}", include_in_schema=False)
    async def resource_type_by_id(provider_id: str, type_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        if type_id != "User":
            return _error(404, "No such resource type.")
        return JSONResponse(_user_type(_base(request, provider_id)), media_type=MEDIA)

    async def _search(provider_id: str, request: Request, filter, startIndex, count,  # noqa: A002, N803
                      attributes, excludedAttributes):  # noqa: N803
        from scim2_models import InvalidFilterException, ScimFilter, User

        base = _base(request, provider_id)
        rows = await run_in_threadpool(store.accounts_for, hub_dir, provider_id)
        items = [_view(a, base) for a in rows]
        if filter:
            try:
                flt = ScimFilter[User](filter)
                items = [i for i in items
                         if flt.match(User.model_validate({k: v for k, v in i.items() if k != "meta"}))]
            except (InvalidFilterException, ValueError, TypeError):
                return _error(400, "The filter could not be read.", "invalidFilter")
        total = len(items)
        start = max(1, int(startIndex or 1))
        page = items[start - 1:start - 1 + max(0, min(int(count if count is not None else 100), 200))]
        page = [_project(i, attributes, excludedAttributes) for i in page]
        return JSONResponse({"schemas": [LIST], "totalResults": total, "startIndex": start,
                             "itemsPerPage": len(page), "Resources": page}, media_type=MEDIA)

    @app.get("/scim/v2/{provider_id}/Users", include_in_schema=False)
    async def list_users(provider_id: str, request: Request, filter: str | None = None,  # noqa: A002
                         startIndex: int = 1, count: int = 100,  # noqa: N803
                         attributes: str | None = None, excludedAttributes: str | None = None):  # noqa: N803
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        return await _search(provider_id, request, filter, startIndex, count, attributes,
                             excludedAttributes)

    @app.post("/scim/v2/{provider_id}/Users/.search", include_in_schema=False)
    @app.post("/scim/v2/{provider_id}/.search", include_in_schema=False)
    async def search_users(provider_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _error(400, "Send a SearchRequest.", "invalidSyntax")
        def joined(v):
            return ",".join(v) if isinstance(v, list) else v
        return await _search(provider_id, request, body.get("filter"), body.get("startIndex", 1),
                             body.get("count", 100), joined(body.get("attributes")),
                             joined(body.get("excludedAttributes")))

    @app.post("/scim/v2/{provider_id}/Users", include_in_schema=False)
    async def create_user(provider_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        try:
            user, clean = _parse_user(await request.json())
        except Exception:  # noqa: BLE001
            return _error(400, "The user resource is not valid.", "invalidValue")
        if not user.user_name:
            return _error(400, "userName is required.", "invalidValue")
        email = _primary_email(user) or (user.user_name.lower() if "@" in user.user_name else None)
        if not email:
            # A directory person without an email still gets an account, under
            # an internal address that receives nothing (they sign in by SAML).
            import re

            local = re.sub(r"[^a-z0-9._-]", "-", user.user_name.lower())[:60] or "user"
            email = f"{local}@{provider_id}.directory.invalid"
        try:
            account = await run_in_threadpool(
                lambda: store.provision(hub_dir, provider_id, user_name=user.user_name, email=email,
                                        name=_display(user), external_id=user.external_id,
                                        active=user.active is not False, resource=clean))
        except store.DirectoryError as exc:
            return _error(exc.status, exc.message, "uniqueness" if exc.status == 409 else None)
        base = _base(request, provider_id)
        view = _view(account, base)
        return JSONResponse(view, status_code=201, media_type=MEDIA,
                            headers={"Location": view["meta"]["location"],
                                     "ETag": view["meta"]["version"]})

    @app.get("/scim/v2/{provider_id}/Users/{scim_id}", include_in_schema=False)
    async def get_user(provider_id: str, scim_id: str, request: Request,
                       attributes: str | None = None, excludedAttributes: str | None = None):  # noqa: N803
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        account = await run_in_threadpool(store.get_account, hub_dir, provider_id, scim_id)
        if account is None:
            return _error(404, "No such user.")
        view = _view(account, _base(request, provider_id))
        return JSONResponse(_project(view, attributes, excludedAttributes), media_type=MEDIA,
                            headers={"ETag": view["meta"]["version"]})

    async def _replace(provider_id: str, scim_id: str, request: Request, body: dict,
                       version: int):
        try:
            user, clean = _parse_user(body)
        except Exception:  # noqa: BLE001
            return _error(400, "The user resource is not valid.", "invalidValue")
        if not user.user_name:
            return _error(400, "userName is required.", "invalidValue")
        try:
            account = await run_in_threadpool(
                lambda: store.update(hub_dir, provider_id, scim_id, user_name=user.user_name,
                                     name=_display(user), external_id=user.external_id,
                                     active=user.active is not False, resource=clean,
                                     expected_version=version))
        except store.DirectoryError as exc:
            return _error(exc.status, exc.message, "uniqueness" if exc.status == 409 else None)
        view = _view(account, _base(request, provider_id))
        return JSONResponse(view, media_type=MEDIA, headers={"ETag": view["meta"]["version"]})

    def _precondition(request: Request, account: dict) -> bool:
        wanted = request.headers.get("if-match")
        return not wanted or wanted.strip() in ("*", f'W/"{account["version"]}"')

    @app.put("/scim/v2/{provider_id}/Users/{scim_id}", include_in_schema=False)
    async def put_user(provider_id: str, scim_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        account = await run_in_threadpool(store.get_account, hub_dir, provider_id, scim_id)
        if account is None:
            return _error(404, "No such user.")
        if not _precondition(request, account):
            return _error(412, "The user changed since you read it.")
        return await _replace(provider_id, scim_id, request, await request.json(),
                              account["version"])

    @app.patch("/scim/v2/{provider_id}/Users/{scim_id}", include_in_schema=False)
    async def patch_user(provider_id: str, scim_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        from scim2_models import PatchOp, User

        account = await run_in_threadpool(store.get_account, hub_dir, provider_id, scim_id)
        if account is None:
            return _error(404, "No such user.")
        if not _precondition(request, account):
            return _error(412, "The user changed since you read it.")
        current = _view(account, "")
        current.pop("meta", None)
        try:
            resource = User.model_validate(current)
            PatchOp[User].model_validate(await request.json()).patch(resource)
        except Exception:  # noqa: BLE001
            return _error(400, "The PATCH operations could not be applied.", "invalidValue")
        body = resource.model_dump(mode="json", by_alias=True, exclude_none=True)
        return await _replace(provider_id, scim_id, request, body, account["version"])

    @app.delete("/scim/v2/{provider_id}/Users/{scim_id}", include_in_schema=False)
    async def delete_user(provider_id: str, scim_id: str, request: Request):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        done = await run_in_threadpool(store.deprovision, hub_dir, provider_id, scim_id)
        if not done:
            return _error(404, "No such user.")
        return Response(status_code=204)

    @app.api_route("/scim/v2/{provider_id}/Groups{rest:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def groups(provider_id: str, request: Request, rest: str = ""):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        return _error(501, "Groups are not supported yet. Provision users only.")

    @app.api_route("/scim/v2/{provider_id}/{rest:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def unknown(provider_id: str, request: Request, rest: str = ""):
        if (refused := await guard(request, provider_id)) is not None:
            return refused
        if rest.split("/")[0] in ("ServiceProviderConfig", "ResourceTypes", "Schemas"):
            return _error(405, "These endpoints are read-only.")
        return _error(404, "No such SCIM endpoint.")
