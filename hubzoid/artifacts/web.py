# Hubzoid published artifacts. Apache-2.0 licensed like the rest of the repository.
"""The report viewer and its API, served under `/portal` by any bridge.

  GET    /portal/artifacts/<id>                 viewer page (sign-in redirect)
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
Console privilege is involved: owners manage their own reports here.

Isolation. The viewer page and the Open WebUI app share an origin behind the
edge. Report HTML is served with CSP `sandbox` (no `allow-same-origin`) and
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
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import artifacts as arts
from . import pages

log = logging.getLogger("hubzoid.artifacts")

_UNAVAILABLE = "This report is not available, or you do not have access to it."
_LINK_BAD = "This link does not work. It may have expired or been turned off."
_ORIGIN = re.compile(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?$")


class ShareEntry(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    kind: str = Field(pattern="^(user|group)$")
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
            raise HTTPException(401, "Sign in to view this report.")
        return subject

    def meta(art: arts.Artifact, who: str, *, public: bool) -> dict:
        base = f"/portal/p/{art.id}" if public else f"/portal/artifacts/{art.id}"
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
        if who == "owner" and not public:
            managed = arts.hub_managed(hub_dir, art.hub)
            out["sharing"] = dict(
                audience=art.audience, people=arts.shares(hub_dir, art.id),
                link=arts.active_link(hub_dir, art.id),
                can_public_link=arts.can_create_link(hub_dir, art, art.owner),
                public_link_hint=_public_link_hint(),
                hub_managed=managed, default_days=arts.link_days(),
                unmanaged_note=("Not available: this agent's access is still managed in "
                                "the chat app." if not managed else ""))
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
            return _message("Report not found", _UNAVAILABLE, 404)
        subject = signed_in(request)
        if not subject:
            # Built only from the validated id: never an open redirect.
            back = f"/portal/artifacts/{artifact_id}"
            return RedirectResponse(f"/auth?redirect={quote(back, safe='/')}", status_code=302)
        art = arts.get(hub_dir, artifact_id)
        if art is None or arts.role(hub_dir, art, subject) is None:
            return _message("Report not available",
                            f"{_UNAVAILABLE} You are signed in as {subject}. If you should "
                            "see it, ask its owner to share it with you.", 404)
        nonce = secrets.token_urlsafe(16)
        return _page(pages.shell(mode="auth", api=f"/portal/artifacts/api/{artifact_id}",
                                 nonce=nonce), nonce)

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
