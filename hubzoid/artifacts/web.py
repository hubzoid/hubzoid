# Hubzoid published artifacts. Apache-2.0 licensed like the rest of the repository.
"""The artifact viewer and its API, served under `/portal` by any bridge.

  GET    /portal/artifacts/<id>                 viewer page (sign-in redirect)
  GET    /portal/latest/<hub>/<key>             the newest artifact with that key
  GET    /portal/artifacts/<id>/content         the file, inline, isolated
  GET    /portal/artifacts/<id>/download        the file, as a download
  GET    /portal/artifacts/api/<id>             metadata (+ sharing, for the owner)
  POST   /portal/artifacts/api/<id>/audience    owner: only me / people / hub
  POST   /portal/artifacts/api/<id>/link        owner: create or revoke a public link
  DELETE /portal/artifacts/api/<id>             owner: delete
  GET    /portal/p/                              public-link page (token in the #fragment)
  POST   /portal/p/open                          exchange the token for a view cookie
  GET    /portal/p/<id>/content|download         the file, with that cookie

Identity comes only from the Open WebUI session, verified server-side
(`access.session.verified_email`). Every request re-decides access through
`artifacts.role`/`open_link`, so a revoked share or link stops at once. No
Console privilege is involved: owners manage their own artifacts here.

Isolation. The viewer page and the Open WebUI app share an origin behind the
edge. Artifact HTML is served with CSP `sandbox` (no `allow-same-origin`) and
framed with the `sandbox` attribute, so it runs in an opaque origin: it cannot
read the session cookie, call the app's APIs with it, or reach the toolbar. By
default it cannot make network requests either (`HUBZOID_ARTIFACT_ALLOW_ORIGINS`
opens named origins). Types the viewer does not preview are served only as
downloads, never rendered in the application origin.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import artifacts as arts
from . import pages
from . import series as series_lib

log = logging.getLogger("hubzoid.artifacts")

_UNAVAILABLE = "This artifact is not available, or you do not have access to it."
_LINK_BAD = "This link does not work. It may have expired or been turned off."
_ORIGIN = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?$")
_REF = re.compile(r"^[0-9a-f]{12}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{12,64}$")


class ShareEntry(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    kind: str = Field(default="user", pattern="^user$")
    principal: str = Field(min_length=1, max_length=320)


class AudienceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: str = Field(pattern="^(owner|people|hub)$")
    people: list[ShareEntry] = Field(default_factory=list, max_length=arts.MAX_SHARES + 1)


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(create|revoke)$")
    days: int | None = Field(default=None, ge=1, le=arts.MAX_LINK_DAYS)


class OpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)


class CompanyLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(on|off)$")


class ScheduleAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(pause|resume|stop)$")


_REPORT_GONE = "This report isn't available"


def _extra_origins() -> str:
    raw = os.environ.get("HUBZOID_ARTIFACT_ALLOW_ORIGINS", "")
    good = [o.strip() for o in raw.split(",") if _ORIGIN.match(o.strip())]
    return (" " + " ".join(good)) if good else ""


def html_csp() -> str:
    extra = _extra_origins()
    return ("sandbox allow-scripts allow-popups allow-modals allow-downloads; "
            f"default-src 'none'; script-src 'unsafe-inline'{extra}; "
            f"style-src 'unsafe-inline'{extra}; img-src data: blob:{extra}; "
            f"font-src data:{extra}; media-src data: blob:; connect-src 'none'; "
            "form-action 'none'; base-uri 'none'; frame-ancestors 'self'")


_PAGE_CSP = ("default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; "
             "img-src 'self' data:; frame-src 'self'; connect-src 'self'; "
             "form-action 'self'; base-uri 'none'; frame-ancestors 'self'")


def _page(body: str, nonce: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status, headers={
        "Content-Security-Policy": _PAGE_CSP.format(n=nonce),
        "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN"})


def _message(title: str, body: str, status: int) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    return _page(pages.message(title=title, body=body, nonce=nonce), nonce, status)


def _public_link_hint() -> str:
    """Why public sharing is off, naming the capability as the Console shows it."""
    return (f"Needs \u201c{arts.SHARE_PUBLIC.label}\u201d. Ask an administrator of this agent "
            "to grant it to you.")


def serve(path: Path, art: arts.Artifact, *, download: bool) -> FileResponse:
    """The stored file with headers that keep it out of the application origin."""
    headers = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
               "Cache-Control": "private, no-store"}
    kind = art.kind
    media = art.content_type
    name = quote(art.filename)
    if download or kind == "download":
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{name}"
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return FileResponse(str(path), media_type=media, headers=headers)
    headers["Content-Disposition"] = f"inline; filename*=UTF-8''{name}"
    if kind == "html":
        headers["Content-Security-Policy"] = html_csp()
        media = "text/html; charset=utf-8"
    elif kind == "pdf":
        # Browsers refuse to render a sandboxed PDF; their viewer runs apart
        # from the page. Framing stays limited to our own viewer.
        headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        headers["X-Frame-Options"] = "SAMEORIGIN"
        media = "application/pdf"
    elif kind in ("image", "svg"):
        headers["Content-Security-Policy"] = ("sandbox; default-src 'none'; img-src data:; "
                                              "style-src 'unsafe-inline'; frame-ancestors 'self'")
    else:  # csv / text: the viewer previews these from JSON; raw bytes as text
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        media = "text/plain; charset=utf-8"
    return FileResponse(str(path), media_type=media, headers=headers)


def _secure(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    return proto == "https"


def build_router(hub_dir) -> APIRouter:
    from ..access.session import require_same_origin, verified_email

    hub_dir = Path(hub_dir)
    router = APIRouter(tags=["artifacts"])

    def signed_in(request: Request) -> str:
        return verified_email(request, hub_dir)

    def load(artifact_id: str, subject: str) -> tuple[arts.Artifact, str]:
        art = arts.get(hub_dir, artifact_id)
        who = arts.role(hub_dir, art, subject) if art else None
        if who is None:
            raise HTTPException(404, _UNAVAILABLE)
        return art, who

    def api_subject(request: Request) -> str:
        subject = signed_in(request)
        if not subject:
            raise HTTPException(401, "Sign in to view this artifact.")
        return subject

    def meta(art: arts.Artifact, who: str, *, public: bool, base: str | None = None) -> dict:
        base = base or (f"/portal/p/{art.id}" if public else f"/portal/artifacts/{art.id}")
        # The file name ends the content URL: a browser's PDF viewer titles the
        # document from its URL, and "content" told people nothing.
        out = dict(id=art.id, title=art.title, filename=art.filename,
                   content_type=art.content_type, size=art.size, created=art.created,
                   kind=art.kind, role=who,
                   content_url=f"{base}/content/{quote(art.filename, safe='')}",
                   download_url=f"{base}/download")
        try:
            out["preview"] = arts.preview(hub_dir, art)
        except arts.ArtifactError:
            out["preview"] = None
        if who == "owner" and not public and base.startswith("/portal/artifacts/"):
            out["sharing"] = dict(
                audience=art.audience, people=arts.shares(hub_dir, art.id),
                link=arts.active_link(hub_dir, art.id),
                can_public_link=arts.can_create_link(hub_dir, art, art.owner),
                public_link_hint=_public_link_hint(),
                default_days=arts.link_days())
        return out

    def refused(exc: arts.ArtifactError):
        raise HTTPException(exc.status, exc.message)

    # ---- JSON API (declared first; its paths never match the viewer's) ---------

    @router.get("/portal/artifacts/api/{artifact_id}")
    def api_get(artifact_id: str, request: Request):
        art, who = load(artifact_id, api_subject(request))
        return meta(art, who, public=False)

    @router.post("/portal/artifacts/api/{artifact_id}/audience")
    def api_audience(artifact_id: str, payload: AudienceRequest, request: Request):
        require_same_origin(request)
        subject = api_subject(request)
        art, _ = load(artifact_id, subject)
        try:
            arts.set_audience(hub_dir, art, subject, payload.audience,
                              [p.model_dump() for p in payload.people])
        except arts.ArtifactError as exc:
            refused(exc)
        return {"ok": True}

    @router.post("/portal/artifacts/api/{artifact_id}/link")
    def api_link(artifact_id: str, payload: LinkRequest, request: Request):
        require_same_origin(request)
        subject = api_subject(request)
        art, _ = load(artifact_id, subject)
        try:
            if payload.action == "revoke":
                arts.revoke_links(hub_dir, art, subject)
                return {"ok": True}
            return arts.create_link(hub_dir, art, subject, days=payload.days)
        except arts.ArtifactError as exc:
            refused(exc)

    @router.delete("/portal/artifacts/api/{artifact_id}")
    def api_delete(artifact_id: str, request: Request):
        require_same_origin(request)
        subject = api_subject(request)
        art, _ = load(artifact_id, subject)
        try:
            arts.delete(hub_dir, art, subject)
        except arts.ArtifactError as exc:
            refused(exc)
        return {"ok": True}

    # ---- signed-in viewer ------------------------------------------------------

    @router.get("/portal/artifacts/{artifact_id}")
    def viewer(artifact_id: str, request: Request):
        if not arts.ID_RE.match(artifact_id):
            return _message("Artifact not found", _UNAVAILABLE, 404)
        subject = signed_in(request)
        if not subject:
            # Built only from the validated id: never an open redirect.
            back = f"/portal/artifacts/{artifact_id}"
            return RedirectResponse(f"/auth?redirect={quote(back, safe='/')}", status_code=302)
        art = arts.get(hub_dir, artifact_id)
        if art is None or arts.role(hub_dir, art, subject) is None:
            return _message("Artifact not available",
                            f"{_UNAVAILABLE} You are signed in as {subject}. If you should "
                            "see it, ask its owner to share it with you.", 404)
        nonce = secrets.token_urlsafe(16)
        return _page(pages.shell(mode="auth", api=f"/portal/artifacts/api/{artifact_id}",
                                 nonce=nonce), nonce)

    @router.get("/portal/latest/{hub}/{key}")
    def newest(hub: str, key: str, request: Request):
        """One bookmark for a board a workflow republishes: the newest artifact
        published with this key, if this viewer may open that one. Never an
        older copy. A team report series is served right here, so the address
        stays the live link."""
        if not arts.HUB_RE.match(hub) or not arts.KEY_RE.match(key):
            return _message("Artifact not found", _UNAVAILABLE, 404)
        subject = signed_in(request)
        if not subject:
            # Built only from the validated hub and key: never an open redirect.
            back = f"/portal/latest/{hub}/{key}"
            return RedirectResponse(f"/auth?redirect={quote(back, safe='/')}", status_code=302)
        found = series_lib.team(hub_dir, hub, key)
        if found is not None and found.last_artifact:
            return series_page(found, subject)
        art = arts.latest(hub_dir, hub, key)
        if art is None or arts.role(hub_dir, art, subject) is None:
            return _message("Artifact not available",
                            f"{_UNAVAILABLE} You are signed in as {subject}. If you should "
                            "see it, ask its owner to share it with you.", 404)
        return RedirectResponse(f"/portal/artifacts/{art.id}", status_code=302,
                                headers={"Cache-Control": "private, no-store"})

    # ---- report series: live latest links, Update now, company links ------------

    def series_page(found, subject: str):
        art = arts.get(hub_dir, found.last_artifact)
        if art is None or arts.role(hub_dir, art, subject) is None:
            return _message("Artifact not available",
                            f"{_UNAVAILABLE} You are signed in as {subject}. If you should "
                            "see it, ask its owner to share it with you.", 404)
        nonce = secrets.token_urlsafe(16)
        return _page(pages.shell(mode="series", api=f"/portal/series/api/{found.id}", nonce=nonce),
                     nonce)

    @router.get("/portal/latest/{hub}/{key}/by/{ref}")
    def newest_personal(hub: str, key: str, ref: str, request: Request):
        """A person's own report series: the newest copy, for whoever may open it."""
        if not arts.HUB_RE.match(hub) or not arts.KEY_RE.match(key) or not _REF.match(ref):
            return _message("Artifact not found", _UNAVAILABLE, 404)
        subject = signed_in(request)
        if not subject:
            back = f"/portal/latest/{hub}/{key}/by/{ref}"
            return RedirectResponse(f"/auth?redirect={quote(back, safe='/')}", status_code=302)
        found = series_lib.personal(hub_dir, hub, key, ref)
        if found is None or not found.last_artifact:
            return _message("Artifact not available", f"{_UNAVAILABLE} You are signed in as {subject}.", 404)
        return series_page(found, subject)

    def series_for(series_id: str, subject: str):
        found = series_lib.get(hub_dir, series_id)
        art = arts.get(hub_dir, found.last_artifact) if found and found.last_artifact else None
        who = arts.role(hub_dir, art, subject) if art else None
        if who is None:
            raise HTTPException(404, _UNAVAILABLE)
        return found, art, who

    def can_update(found, subject: str) -> bool:
        from ..access import store_for

        if found.team:   # a stopped team page isn't made by the workflow any more
            return bool(found.live) and bool(store_for(hub_dir).can(subject, found.hub, "workflows_manage"))
        return subject == found.owner

    def series_info(found, subject: str | None, *, reader: bool) -> dict:
        from . import domains

        state = series_lib.state(hub_dir, found)
        info = dict(id=found.id, scope=found.scope, title=found.title or found.key, **state)
        if not reader and subject:
            info["can_update"] = can_update(found, subject)
            wait = series_lib.UPDATE_COOLDOWN - (time.time() - (found.last_touch or 0))
            info["update_wait"] = max(0, int(wait)) if found.last_touch else 0
        if not reader and subject and not found.team and subject == found.owner:
            token = series_lib.domain_token(hub_dir, found)
            from ..access import store_for

            info["company"] = dict(
                available=domains.enabled(), domains=domains.label(),
                allowed=bool(store_for(hub_dir).can(subject, found.hub, arts.SHARE_DOMAIN.permission)),
                on=bool(token), url=series_lib.share_url(hub_dir, token) if token else None,
                reason=domains.status()[1])
        return info

    @router.get("/portal/series/api/{series_id}")
    def series_api(series_id: str, request: Request):
        subject = api_subject(request)
        found, art, who = series_for(series_id, subject)
        return {"series": series_info(found, subject, reader=False),
                **meta(art, who, public=False)}

    @router.get("/portal/series/api/{series_id}/latest")
    def series_latest(series_id: str, request: Request):
        subject = api_subject(request)
        found, art, _ = series_for(series_id, subject)
        return _latest_json(request, found, art)

    @router.post("/portal/series/api/{series_id}/update")
    def series_update(series_id: str, request: Request):
        require_same_origin(request)
        subject = api_subject(request)
        found, _, _ = series_for(series_id, subject)
        if not can_update(found, subject):
            raise HTTPException(403, "Only the report's owner, or someone who runs this agent's "
                                     "workflows, can update it.")
        wait = series_lib.UPDATE_COOLDOWN - (time.time() - (found.last_touch or 0))
        if found.last_touch and wait > 0:
            minutes = max(1, int((time.time() - found.last_touch) // 60))
            raise HTTPException(429, f"Updated {minutes} minute{'s' if minutes != 1 else ''} ago. You "
                                     f"can update again in {max(1, int(wait // 60) + 1)} minutes.")
        try:
            run_id = _start_update(hub_dir, found, subject)
        except _UpdateRefused as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True, "run_id": run_id}

    @router.post("/portal/series/api/{series_id}/company")
    def series_company(series_id: str, payload: CompanyLinkRequest, request: Request):
        """The owner turns a personal report's company link on or off."""
        from . import domains
        from ..access import store_for

        require_same_origin(request)
        subject = api_subject(request)
        found, _, who = series_for(series_id, subject)
        if found.team or subject != found.owner or who != "owner":
            raise HTTPException(404, _UNAVAILABLE)
        if payload.action == "off":
            series_lib.domain_off(hub_dir, found, subject)
            return {"ok": True, "on": False}
        on, why = domains.status()
        if not on:
            raise HTTPException(409, why)
        if not store_for(hub_dir).can(subject, found.hub, arts.SHARE_DOMAIN.permission):
            raise HTTPException(403, f"You can't share reports with {domains.label()}. An admin can "
                                     f"allow it in the Console (\u201c{arts.SHARE_DOMAIN.label}\u201d).")
        token = series_lib.domain_on(hub_dir, found, subject)
        return {"ok": True, "on": True, "url": series_lib.share_url(hub_dir, token)}

    # Company links: the token in the path is checked on every request, with
    # the signed-in account's email domain. One page for every refusal.

    def company(token: str, request: Request):
        """(series, newest copy, subject) or an HTTP error. Readers who don't
        use the agent are marked reader-only the first time."""
        from . import domains
        from ..access import store_for

        subject = signed_in(request)
        found = series_lib.for_token(hub_dir, token)
        if not subject:
            raise HTTPException(401, "Sign in to open this report.")
        if found is None or not found.domain_on or not found.last_artifact:
            raise HTTPException(404, _REPORT_GONE)
        art = arts.get(hub_dir, found.last_artifact)
        if art is None or not arts._active(hub_dir, subject):
            raise HTTPException(404, _REPORT_GONE)
        if arts.role(hub_dir, art, subject) is not None:
            return found, art, subject, False
        if not domains.email_allowed(subject):
            raise HTTPException(404, _REPORT_GONE)
        if not found.team and not arts._owner_current(hub_dir, art):
            raise HTTPException(404, _REPORT_GONE)
        gs = store_for(hub_dir)
        if not gs.permissions_for(subject, found.hub) and not gs.is_reader(subject):
            gs.set_reader(subject)
        return found, art, subject, True

    @router.get("/portal/r/{token}")
    def company_page(token: str, request: Request):
        if not _TOKEN.match(token):
            return _message(_REPORT_GONE, "The link may be turned off or incomplete.", 404)
        if not signed_in(request):
            return RedirectResponse(f"/auth?redirect={quote('/portal/r/' + token, safe='/')}",
                                    status_code=302)
        nonce = secrets.token_urlsafe(16)
        try:
            company(token, request)
        except HTTPException:
            return _page(pages.unavailable(email=signed_in(request), back=f"/portal/r/{token}",
                                           nonce=nonce), nonce, 404)
        return _page(pages.shell(mode="company", api=f"/portal/r/{token}/api", nonce=nonce), nonce)

    @router.get("/portal/r/{token}/api")
    def company_api(token: str, request: Request):
        found, art, subject, reader = company(token, request)
        base = f"/portal/r/{token}"
        out = meta(art, "viewer", public=False, base=base)
        out["series"] = series_info(found, subject, reader=True)
        return out

    @router.get("/portal/r/{token}/api/latest")
    def company_latest(token: str, request: Request):
        found, art, _, _ = company(token, request)
        return _latest_json(request, found, art)

    @router.get("/portal/r/{token}/content/{_filename}")
    def company_content(token: str, _filename: str, request: Request):
        _, art, _, _ = company(token, request)
        try:
            return serve(arts.content_path(hub_dir, art), art, download=False)
        except arts.ArtifactError as exc:
            refused(exc)

    @router.get("/portal/r/{token}/download")
    def company_download(token: str, request: Request):
        _, art, _, _ = company(token, request)
        try:
            return serve(arts.content_path(hub_dir, art), art, download=True)
        except arts.ArtifactError as exc:
            refused(exc)

    # ---- a schedule's owner page (linked from the "New schedule" email) --------

    @router.get("/portal/my/schedules/{schedule_id}")
    def my_schedule_page(schedule_id: str, request: Request):
        subject = signed_in(request)
        if not subject:
            back = f"/portal/my/schedules/{quote(schedule_id, safe='')}"
            return RedirectResponse(f"/auth?redirect={quote(back, safe='/')}", status_code=302)
        nonce = secrets.token_urlsafe(16)
        sched = _own_schedule(schedule_id, subject)
        if sched is None:
            return _message("Schedule not available", "This schedule isn't yours, or it no longer exists.", 404)
        return _page(pages.schedule_page(view=_schedule_view(sched), nonce=nonce), nonce)

    @router.post("/portal/my/schedules/api/{schedule_id}")
    def my_schedule_action(schedule_id: str, payload: ScheduleAction, request: Request):
        from ..workflows import personal

        require_same_origin(request)
        subject = api_subject(request)
        sched = _own_schedule(schedule_id, subject)
        if sched is None:
            raise HTTPException(404, "This schedule isn't yours, or it no longer exists.")
        try:
            if payload.action == "stop":
                personal._update(hub_dir, sched.id, state="stopped", reason="owner", paused_by=subject,
                                 next_fire=None)
            else:
                personal.set_paused(hub_dir, sched.hub, subject, sched.id, payload.action == "pause")
        except personal.ScheduleError as exc:
            raise HTTPException(409, str(exc))
        return _schedule_view(personal.get(hub_dir, sched.id))

    def _own_schedule(schedule_id: str, subject: str):
        from ..workflows import personal

        sched = personal.get(hub_dir, schedule_id)
        if sched is None or sched.owner != subject or sched.state == "stopped":
            return None
        return sched

    def _schedule_view(sched) -> dict:
        from ..workflows import personal
        from ..workflows.runtime import title_for

        return {"name": sched.name, "report": title_for(sched.workflow),
                "when": f"{personal.cadence_words(sched)} ({sched.timezone})",
                "state": personal.state_words(sched), "paused": sched.state == "paused",
                "stopped": sched.state == "stopped", "last": personal.last_words(sched),
                "api": f"/portal/my/schedules/api/{sched.id}"}

    @router.get("/portal/artifacts/{artifact_id}/content")
    @router.get("/portal/artifacts/{artifact_id}/content/{_filename}")
    def content(artifact_id: str, request: Request, _filename: str = ""):
        art, _ = load(artifact_id, api_subject(request))
        try:
            return serve(arts.content_path(hub_dir, art), art, download=False)
        except arts.ArtifactError as exc:
            refused(exc)

    @router.get("/portal/artifacts/{artifact_id}/download")
    def download(artifact_id: str, request: Request):
        art, _ = load(artifact_id, api_subject(request))
        try:
            return serve(arts.content_path(hub_dir, art), art, download=True)
        except arts.ArtifactError as exc:
            refused(exc)

    # ---- public links ----------------------------------------------------------

    @router.get("/portal/p/")
    @router.get("/portal/p")
    def public_page():
        nonce = secrets.token_urlsafe(16)
        return _page(pages.shell(mode="public", api=None, nonce=nonce), nonce)

    @router.post("/portal/p/open")
    def public_open(payload: OpenRequest, request: Request):
        require_same_origin(request)
        found = arts.open_link(hub_dir, payload.token)
        if found is None:
            raise HTTPException(404, _LINK_BAD)
        art, link_id = found
        resp = JSONResponse(meta(art, "viewer", public=True),
                            headers={"Cache-Control": "private, no-store"})
        resp.set_cookie(f"hz_pl_{art.id}", arts.view_cookie(hub_dir, art.id, link_id),
                        max_age=arts.VIEW_SECONDS, path="/portal/p/", httponly=True,
                        samesite="strict", secure=_secure(request))
        return resp

    def public_art(artifact_id: str, request: Request) -> arts.Artifact:
        art = arts.check_view_cookie(hub_dir, artifact_id,
                                     request.cookies.get(f"hz_pl_{artifact_id}"))
        if art is None:
            raise HTTPException(404, _LINK_BAD)
        return art

    @router.get("/portal/p/{artifact_id}/content")
    @router.get("/portal/p/{artifact_id}/content/{_filename}")
    def public_content(artifact_id: str, request: Request, _filename: str = ""):
        art = public_art(artifact_id, request)
        try:
            return serve(arts.content_path(hub_dir, art), art, download=False)
        except arts.ArtifactError as exc:
            refused(exc)

    @router.get("/portal/p/{artifact_id}/download")
    def public_download(artifact_id: str, request: Request):
        art = public_art(artifact_id, request)
        try:
            return serve(arts.content_path(hub_dir, art), art, download=True)
        except arts.ArtifactError as exc:
            refused(exc)

    return router


class _UpdateRefused(Exception):
    """Update now can't start; the message is safe to show."""


def _latest_json(request: Request, found, art) -> JSONResponse:
    """What an open tab polls: the newest copy's id and time, with an ETag so
    an unchanged report costs a 304."""
    etag = f'"{art.id}"'
    if request.headers.get("if-none-match") == etag:
        return JSONResponse(None, status_code=304, headers={"ETag": etag})
    return JSONResponse({"artifact": art.id, "updated": found.updated_at, "live": bool(found.live)},
                        headers={"ETag": etag, "Cache-Control": "private, no-cache"})


def _start_update(hub_dir: Path, found, subject: str) -> str:
    """Rerun the run that made the newest copy, with the same inputs: as the
    owner for a personal report, as the workflow's account for a team one."""
    from .. import deployment
    from ..workflows import client, runtime

    env = {"trigger": "update", "scope": found.scope, "started_by": subject,
           "inputs": found.inputs, "inputs_display": ""}
    if not found.team:
        env.update(acts_as=found.owner, schedule_id=found.schedule_id)
    try:
        series_hub = Path(deployment.hub_path(hub_dir, found.hub))
    except (KeyError, OSError, ValueError):
        series_hub = Path(hub_dir)
    if runtime._LAUNCHED and runtime._HUB_DIR and Path(runtime._HUB_DIR).resolve() == series_hub.resolve():
        if found.workflow not in runtime._REGISTRY:
            raise _UpdateRefused("This report's workflow isn't running here right now.")
        handle = runtime.start_run(found.workflow, env) if not found.team else \
            runtime.start(found.workflow, envelope=env)
        return handle.get_workflow_id()
    live = client.enqueue_live(series_hub, found.workflow, envelope=env)
    if live is None:
        raise _UpdateRefused("Reports can't run on this agent right now. Try again later.")
    run_id = live.get_workflow_id()
    live.client.destroy()
    return run_id
