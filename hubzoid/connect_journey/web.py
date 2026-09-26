"""The connection pages: `/portal/connect/<id>` and its actions.

Every page is bound to the journey's subject: it needs a signed-in Open WebUI
session (checked server-side by `access.session.verified_email`) whose email is
the person who asked. A signed-out visitor is sent to sign in and brought back
to the same page. Another account gets 403 and the attempt is audited.
Mutations (`start`, `cancel`) also require the same Origin. The pages use
`Referrer-Policy: same-origin` so the browser sends that Origin on its own form
posts (with `no-referrer` it sends `Origin: null`, which is refused), while a
navigation to another site still carries no referrer.

The result shown on `done` and `status` comes only from the provider's own
records (see `providers.py`). Query parameters on the return from the provider
are never read, so a forged or replayed callback cannot claim success.
"""
from __future__ import annotations

import html
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..access.identity import normalize
from . import store
from .providers import JourneyError, label

log = logging.getLogger("hubzoid.connect")

COOKIE = "hz_connect"
POLL_SECONDS = 30


def _headers(nonce: str | None = None) -> dict:
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "same-origin",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'unsafe-inline'; script-src {script}; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"),
    }


_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#5d5d58;--line:#e2e2dc;
--accent:#1f5fbf;--ok:#1e7a46;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1d1d1b;--ink:#ededea;
--muted:#a9a9a3;--line:#2f2f2c;--accent:#7fb0ff;--ok:#5fcf8f;--bad:#ff8a80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;min-height:100vh;
align-items:center;justify-content:center;padding:16px}
main{background:var(--card);border:1px solid var(--line);border-radius:12px;max-width:460px;
width:100%;padding:28px}h1{font-size:1.35rem;margin:0 0 8px}p{margin:8px 0;color:var(--muted)}
p.lead{color:var(--ink)}.mark{font-size:2rem;line-height:1;margin-bottom:12px}
.ok .mark{color:var(--ok)}.bad .mark{color:var(--bad)}
form{display:inline}.actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:20px}
button,a.button{font:inherit;border-radius:8px;padding:10px 18px;border:1px solid var(--line);
background:transparent;color:var(--ink);cursor:pointer;text-decoration:none}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media (prefers-color-scheme:dark){button.primary{color:#0b1a33}}
"""


def _page(title: str, paragraphs: list[str], status: int = 200, *, tone: str = "",
          actions: str = "", script: str = "") -> HTMLResponse:
    nonce = secrets.token_urlsafe(12) if script else None
    mark = {"ok": "&#10003;", "bad": "&#10007;"}.get(tone, "")
    body = "".join(
        f'<p class="{"lead" if i == 0 else ""}">{p}</p>' for i, p in enumerate(paragraphs))
    doc = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex\">"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><main class=\"{tone}\" aria-live=\"polite\">"
        + (f'<div class="mark" aria-hidden="true">{mark}</div>' if mark else "")
        + f"<h1>{html.escape(title)}</h1>{body}"
        + (f'<div class="actions">{actions}</div>' if actions else "")
        + "</main>"
        + (f'<script nonce="{nonce}">{script}</script>' if script else "")
        + "</body></html>")
    return HTMLResponse(doc, status_code=status, headers=_headers(nonce))


def _e(value) -> str:
    return html.escape(str(value or ""))


def _not_found() -> HTMLResponse:
    return _page("Link not found",
                 ["This connection link is not valid.",
                  "Ask the agent again for a new link."], 404, tone="bad")


def _gone(j: dict) -> HTMLResponse:
    if j["status"] == "superseded":
        return _page("A newer link was sent",
                     ["This link was replaced by a newer one.",
                      "Use the latest link from the chat."], 410, tone="bad")
    return _page("This link has expired",
                 [f"The link to connect {_e(label(j['app']))} is no longer valid.",
                  "Ask the agent again for a new link."], 410, tone="bad")


def _sign_in_url(jid: str, page: str = "") -> str:
    """Open WebUI's sign-in, returning to this journey afterwards. Built only
    from a journey id that exists, never from the request: no open redirect."""
    return "/auth?redirect=" + quote(f"/portal/connect/{jid}{page}", safe="/")


def _sign_in(jid: str) -> HTMLResponse:
    return _page("Sign in to continue",
                 ["This link is personal. Sign in with the account that asked for it.",
                  "You will come back here after signing in."], 401,
                 actions=f'<a class="button" href="{_sign_in_url(jid)}">Sign in</a>')


def _redirect_to_sign_in(jid: str, page: str = "") -> RedirectResponse:
    return RedirectResponse(_sign_in_url(jid, page), status_code=302,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})


def _outcome(j: dict, email: str) -> HTMLResponse:
    app = _e(label(j["app"]))
    status = j["status"]
    back = ("You will get a confirmation in WhatsApp." if j.get("surface") == "whatsapp"
            else "You can close this tab and return to the chat.")
    if status == "connected":
        return _page(f"{label(j['app'])} is connected",
                     [f"{app} is connected for {_e(email)}.", back], 200, tone="ok")
    if status == "failed":
        return _page(f"{label(j['app'])} was not connected",
                     [f"The connection to {app} did not complete.",
                      "Return to the chat and ask again for a new link."], 200, tone="bad")
    if status == "cancelled":
        return _page("Connection cancelled",
                     [f"{app} was not connected. You can close this tab.",
                      "Ask the agent again whenever you are ready."], 200)
    return _gone(j)


def build_router(hub_dir: Path, *, session_email=None) -> APIRouter:
    router = APIRouter(prefix="/portal/connect")
    hub_dir = Path(hub_dir)

    def email_of(request: Request) -> str:
        if session_email is not None:
            return normalize(session_email(request) or "")
        from ..access.session import verified_email

        return normalize(verified_email(request, hub_dir) or "")

    def bind(request: Request, j: dict):
        """(email, None) for the journey's own signed-in subject, else
        (None, the response to send)."""
        try:
            email = email_of(request)
        except HTTPException as exc:
            return None, _page("Try again shortly",
                               ["Your sign-in could not be checked right now."], exc.status_code)
        if not email:
            return None, _sign_in(j["id"])
        if email != j["subject"]:
            store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                        decision="deny", reason="wrong-account")
            return None, _page("This link is for another account",
                               ["This link was created for a different account, so it "
                                "cannot be used while you are signed in as "
                                f"{_e(email)}.",
                                "Sign in with the account that asked, or ask the agent "
                                "again from your own chat."], 403, tone="bad")
        return email, None

    def fresh(j: dict) -> dict:
        from . import finalize

        return finalize(hub_dir, j)

    def still_permitted(j: dict) -> bool:
        """At start, re-check what can change after the link was sent: a block,
        and on a managed hub the connector grant itself."""
        from ..access import store_for

        from . import capability
        try:
            gs = store_for(hub_dir)
            if gs.is_suspended(j["subject"]):
                return False
            if gs.is_authoritative(j["hub"]):
                return gs.can(j["subject"], j["hub"], capability(j["app"]))
        except Exception:  # noqa: BLE001 — cannot check -> refuse
            log.warning("connect: access re-check failed", exc_info=True)
            return False
        return True

    @router.get("/{jid}")
    def page(jid: str, request: Request):
        j = store.get(hub_dir, jid)
        if j is None:
            return _not_found()
        j = fresh(j)
        if j["status"] in ("expired", "superseded"):
            return _gone(j)
        email, refusal = bind(request, j)
        if refusal is not None:
            # Signed out: sign in, then come straight back to this page.
            return _redirect_to_sign_in(j["id"]) if refusal.status_code == 401 else refusal
        if j["status"] not in store.OPEN:
            return _outcome(j, email)
        app = _e(label(j["app"]))
        base = f"/portal/connect/{quote(jid)}"
        return _page(f"Connect {label(j['app'])}",
                     [f"Connect {app} for {_e(email)}.",
                      f"You will be sent to {app} to approve access, then brought back here."],
                     actions=(f'<form method="post" action="{base}/start">'
                              f'<button class="primary" type="submit">Continue</button></form>'
                              f'<form method="post" action="{base}/cancel">'
                              f'<button type="submit">Cancel</button></form>'))

    @router.post("/{jid}/start")
    def start(jid: str, request: Request):
        from ..access.session import require_same_origin

        from . import providers, ttl
        try:
            require_same_origin(request)
        except HTTPException:
            return _page("Request refused", ["Open the link from the chat and try again."], 403,
                         tone="bad")
        j = store.get(hub_dir, jid)
        if j is None:
            return _not_found()
        j = fresh(j)
        if j["status"] in ("expired", "superseded"):
            return _gone(j)
        email, refusal = bind(request, j)
        if refusal is not None:
            return refusal
        if j["status"] not in store.OPEN:
            return _outcome(j, email)
        if not still_permitted(j):
            store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                        decision="deny", reason="no longer permitted")
            return _page("Not permitted",
                         [f"You no longer have permission to connect {_e(label(j['app']))}.",
                          "Ask your administrator."], 403, tone="bad")
        try:
            target = providers.begin_url(hub_dir, j)
        except JourneyError as err:
            return _page("Cannot start", [_e(err.message)], 410, tone="bad")
        if not store.mark_started(hub_dir, jid, ttl=ttl()):
            return _gone(store.get(hub_dir, jid) or j)
        store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                    decision="started", reason=j["provider"] or "")
        resp = RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store",
                                                                  "Referrer-Policy": "no-referrer"})
        # The edge sends Open WebUI's post-authorization redirect back to our
        # done page while this cookie is present (see edge.py).
        secure = (request.url.scheme == "https"
                  or request.headers.get("x-forwarded-proto", "").lower() == "https")
        resp.set_cookie(COOKIE, jid, max_age=ttl(), path="/", httponly=True,
                        samesite="lax", secure=secure)
        return resp

    @router.post("/{jid}/cancel")
    def cancel(jid: str, request: Request):
        from ..access.session import require_same_origin
        try:
            require_same_origin(request)
        except HTTPException:
            return _page("Request refused", ["Open the link from the chat and try again."], 403,
                         tone="bad")
        j = store.get(hub_dir, jid)
        if j is None:
            return _not_found()
        email, refusal = bind(request, j)
        if refusal is not None:
            return refusal
        if store.transition(hub_dir, jid, frm=store.OPEN, to="cancelled"):
            store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                        decision="cancelled", reason="cancelled on the link page")
        resp = _outcome(store.get(hub_dir, jid) or j, email)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @router.get("/{jid}/done")
    def done(jid: str, request: Request):
        # Query parameters from the provider's redirect are deliberately ignored.
        j = store.get(hub_dir, jid)
        if j is None:
            return _not_found()
        email, refusal = bind(request, j)
        if refusal is not None:
            return (_redirect_to_sign_in(j["id"], "/done") if refusal.status_code == 401
                    else refusal)
        j = fresh(j)
        if j["status"] in store.OPEN:
            app = _e(label(j["app"]))
            status_url = f"/portal/connect/{quote(jid)}/status"
            script = (
                "(function(){var t0=Date.now();function tick(){fetch(" + repr(status_url)
                + ",{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.json()})"
                ".then(function(d){if(d.state&&d.state!=='pending'&&d.state!=='started'){"
                "location.reload();return}next()}).catch(next)}"
                "function next(){if(Date.now()-t0<" + str(POLL_SECONDS * 1000) + "){"
                "setTimeout(tick,2000)}else{var m=document.getElementById('wait');"
                "if(m){m.textContent='The connection has not completed. If you cancelled "
                "or closed the sign-in window, ask the agent again for a new link.'}}}"
                "setTimeout(tick,1500)})();")
            resp = _page("Finishing up",
                         [f'<span id="wait">Checking your {app} connection…</span>',
                          "This page updates by itself."], 202, script=script)
        else:
            resp = _outcome(j, email)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @router.get("/{jid}/status")
    def status(jid: str, request: Request):
        hdrs = {"Cache-Control": "no-store"}
        j = store.get(hub_dir, jid)
        if j is None:
            return JSONResponse({"state": "unknown"}, status_code=404, headers=hdrs)
        try:
            email = email_of(request)
        except HTTPException as exc:
            return JSONResponse({"state": "unavailable"}, status_code=exc.status_code, headers=hdrs)
        if not email:
            return JSONResponse({"state": "sign-in"}, status_code=401, headers=hdrs)
        if email != j["subject"]:
            return JSONResponse({"state": "forbidden"}, status_code=403, headers=hdrs)
        j = fresh(j)
        return JSONResponse({"state": j["status"], "app": j["app"],
                             "checked": int(time.time())}, headers=hdrs)

    return router
