"""The connection pages: `/portal/connect/<id>` and its actions, and
`/portal/connections`, where a signed-in person sees and manages theirs.

Every link page is bound to the journey's account: it needs a signed-in
session whose account id and email are the ones that asked, checked
server-side: a Hubzoid session (`hubzoid.auth.current_user`) in the default UI
mode, a live Open WebUI session check (`access.session.verified_person`) in
Open WebUI mode. A signed-out visitor is sent to sign in and brought back to
the same page. Another account, even one with the same email, gets 403 and the
attempt is audited.
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

POLL_SECONDS = 30
PAGE = "/portal/connections"


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
.who{font-size:.85rem;color:var(--muted);margin:0 0 4px}
table{width:100%;border-collapse:collapse;margin-top:14px}td{padding:10px 4px;
border-top:1px solid var(--line);vertical-align:middle}td:last-child{text-align:right}
.pill{display:inline-block;font-size:.78rem;padding:2px 9px;border-radius:999px;
border:1px solid var(--line);color:var(--muted)}.pill.ok{color:var(--ok);border-color:var(--ok)}
.pill.bad{color:var(--bad);border-color:var(--bad)}
td button{padding:6px 12px;font-size:.9rem}
"""


def _page(title: str, paragraphs: list[str], status: int = 200, *, tone: str = "",
          actions: str = "", script: str = "", eyebrow: str = "",
          extra: str = "") -> HTMLResponse:
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
        + (f'<p class="who">{html.escape(eyebrow)}</p>' if eyebrow else "")
        + f"<h1>{html.escape(title)}</h1>{body}{extra}"
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


def _agent_name(hub_dir: Path, hub: str) -> str:
    """The display name of the agent ``hub`` (a gateway's journey can be for
    another agent than the bridge serving the page)."""
    from .. import deployment

    try:
        for h in deployment.hubs(Path(hub_dir)):
            if normalize(h.get("key", "")) == normalize(hub):
                return h.get("name") or hub
    except Exception:  # noqa: BLE001 — wording only
        log.debug("connect: no agent name for %s", hub, exc_info=True)
    return hub


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
    back = ("You'll get a confirmation in WhatsApp." if j.get("surface") == "whatsapp"
            else "Close this tab and carry on in your chat.")
    if status == "connected":
        return _page(f"{label(j['app'])} connected", [back], 200, tone="ok",
                     actions=f'<a class="button" href="{PAGE}">Your connections</a>')
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
    router = APIRouter()
    hub_dir = Path(hub_dir)

    def legacy() -> bool:
        from .. import appmode

        return appmode.is_openwebui(hub_dir)

    def who_of(request: Request) -> tuple[str, str]:
        """``(account id, email)`` of the signed-in viewer, or ``("", "")``."""
        if session_email is not None:
            email = normalize(session_email(request) or "")
            if not email:
                return "", ""
            from ..connectors import tokens

            return tokens.user_id_for(hub_dir, email) or "", email
        if legacy():
            from ..access.session import verified_person

            who = verified_person(request, hub_dir)
            return (who[0], normalize(who[1])) if who else ("", "")
        from ..auth import current_user

        user = current_user(request, hub_dir)
        request.state.hz_user = user
        return (str(user.id), normalize(user.email)) if user else ("", "")

    def user_of(request: Request, account: str, email: str):
        """The signed-in account, for the connector flow."""
        user = getattr(request.state, "hz_user", None)
        if user is not None:
            return user
        from ..auth import AuthUser

        return AuthUser(id=account, email=email) if account else None

    def bind(request: Request, j: dict):
        """((account, email), None) for the journey's own signed-in account,
        else (None, the response to send). A link made before links recorded
        their account no longer works: the person asks for a new one."""
        try:
            account, email = who_of(request)
        except HTTPException as exc:
            return None, _page("Try again shortly",
                               ["Your sign-in could not be checked right now."], exc.status_code)
        if not email:
            return None, _sign_in(j["id"])
        if email != j["subject"] or not j.get("account") or account != j["account"]:
            store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                        decision="deny", reason="wrong-account")
            return None, _page("This link is for another account",
                               ["This link was created for a different account, so it "
                                "cannot be used while you are signed in as "
                                f"{_e(email)}.",
                                "Sign in with the account that asked, or ask the agent "
                                "again from your own chat."], 403, tone="bad")
        return (account, email), None

    def fresh(j: dict) -> dict:
        from . import finalize

        return finalize(hub_dir, j)

    def still_permitted(j: dict) -> bool:
        """At start, re-check what can change after the link was sent: a block,
        and the connector grant itself."""
        from ..access import store_for

        from . import capability
        try:
            gs = store_for(hub_dir)
            if gs.is_suspended(j["subject"]):
                return False
            return gs.can(j["subject"], j["hub"], capability(j["app"]))
        except Exception:  # noqa: BLE001 — cannot check -> refuse
            log.warning("connect: access re-check failed", exc_info=True)
            return False

    @router.get("/portal/connect/{jid}")
    def page(jid: str, request: Request):
        j = store.get(hub_dir, jid)
        if j is None:
            return _not_found()
        j = fresh(j)
        if j["status"] in ("expired", "superseded"):
            return _gone(j)
        who, refusal = bind(request, j)
        if refusal is not None:
            # Signed out: sign in, then come straight back to this page.
            return _redirect_to_sign_in(j["id"]) if refusal.status_code == 401 else refusal
        email = who[1]
        if j["status"] not in store.OPEN:
            return _outcome(j, email)
        app = _e(label(j["app"]))
        agent = _e(_agent_name(hub_dir, j["hub"]))
        base = f"/portal/connect/{quote(jid)}"
        return _page(f"Connect {label(j['app'])}",
                     [f"{agent} will use {app} as you.", f"Signed in as {_e(email)}"],
                     eyebrow=_agent_name(hub_dir, j["hub"]),
                     actions=(f'<form method="post" action="{base}/start">'
                              f'<button class="primary" type="submit">Continue</button></form>'
                              f'<form method="post" action="{base}/cancel">'
                              f'<button type="submit">Cancel</button></form>'))

    @router.post("/portal/connect/{jid}/start")
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
        who, refusal = bind(request, j)
        if refusal is not None:
            return refusal
        account, email = who
        if j["status"] not in store.OPEN:
            return _outcome(j, email)
        if not still_permitted(j):
            store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                        decision="deny", reason="no longer permitted")
            return _page("Not permitted",
                         [f"You no longer have permission to connect {_e(label(j['app']))}.",
                          "Ask your administrator."], 403, tone="bad")
        try:
            user = user_of(request, account, email)
            if user is None:
                return _page("Cannot start", ["Your account could not be found. Sign in "
                                              "again and reopen the link."], 403, tone="bad")
            target = providers.begin_url(hub_dir, j, request=request, user=user)
        except JourneyError as err:
            return _page("Cannot start", [_e(err.message)], 410, tone="bad")
        if not store.mark_started(hub_dir, jid, ttl=ttl()):
            return _gone(store.get(hub_dir, jid) or j)
        store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                    decision="started", reason=j["provider"] or "")
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store",
                                                                  "Referrer-Policy": "no-referrer"})

    @router.post("/portal/connect/{jid}/cancel")
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
        who, refusal = bind(request, j)
        if refusal is not None:
            return refusal
        email = who[1]
        if store.transition(hub_dir, jid, frm=store.OPEN, to="cancelled"):
            store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                        decision="cancelled", reason="cancelled on the link page")
        return _outcome(store.get(hub_dir, jid) or j, email)

    @router.get("/portal/connect/{jid}/done")
    def done(jid: str, request: Request):
        # Query parameters from the provider's redirect are deliberately ignored.
        j = store.get(hub_dir, jid)
        if j is None:
            return _not_found()
        who, refusal = bind(request, j)
        if refusal is not None:
            return (_redirect_to_sign_in(j["id"], "/done") if refusal.status_code == 401
                    else refusal)
        email = who[1]
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
            return _page("Finishing up",
                         [f'<span id="wait">Checking your {app} connection…</span>',
                          "This page updates by itself."], 202, script=script)
        return _outcome(j, email)

    @router.get("/portal/connect/{jid}/status")
    def status(jid: str, request: Request):
        hdrs = {"Cache-Control": "no-store"}
        j = store.get(hub_dir, jid)
        if j is None:
            return JSONResponse({"state": "unknown"}, status_code=404, headers=hdrs)
        try:
            account, email = who_of(request)
        except HTTPException as exc:
            return JSONResponse({"state": "unavailable"}, status_code=exc.status_code, headers=hdrs)
        if not email:
            return JSONResponse({"state": "sign-in"}, status_code=401, headers=hdrs)
        if email != j["subject"] or not j.get("account") or account != j["account"]:
            return JSONResponse({"state": "forbidden"}, status_code=403, headers=hdrs)
        j = fresh(j)
        return JSONResponse({"state": j["status"], "app": j["app"],
                             "checked": int(time.time())}, headers=hdrs)

    # ---- Your connections ------------------------------------------------------
    @router.get(PAGE)
    def connections(request: Request):
        """Every connector this person may use, with its status and one action.
        Open WebUI mode only: the web app has its own Settings → Connections."""
        if not legacy() and session_email is None:
            return RedirectResponse("/account/connections", status_code=302)
        try:
            account, email = who_of(request)
        except HTTPException as exc:
            return _page("Try again shortly", ["Your sign-in could not be checked right now."],
                         exc.status_code)
        if not email:
            return RedirectResponse("/auth?redirect=" + quote(PAGE, safe="/"), status_code=302,
                                    headers={"Cache-Control": "no-store"})
        from ..access import store_for

        if store_for(hub_dir).is_suspended(email):
            return _page("Your connections", ["Your access is blocked. Contact your administrator."],
                         403, tone="bad")
        rows = []
        for c, status, allowed in _mine(hub_dir, account, email):
            name = _e(c.name)
            action = ""
            if status == "shared":
                pill = '<span class="pill">Shared</span>'
            elif status == "connected":
                pill = '<span class="pill ok">Connected</span>'
                action = _button(f"{PAGE}/{quote(c.id)}/disconnect", "Disconnect")
            elif not allowed:
                # Still connected somewhere it is no longer granted: only remove it.
                pill = '<span class="pill">Not available</span>'
                action = _button(f"{PAGE}/{quote(c.id)}/disconnect", "Disconnect")
            elif status == "expired":
                pill = '<span class="pill bad">Expired</span>'
                action = _button(f"{PAGE}/{quote(c.id)}/connect", "Reconnect", primary=True)
            else:
                pill = '<span class="pill">Not connected</span>'
                action = _button(f"{PAGE}/{quote(c.id)}/connect", "Connect", primary=True)
            rows.append(f"<tr><td><b>{name}</b></td><td>{pill}</td><td>{action}</td></tr>")
        table = f"<table>{''.join(rows)}</table>" if rows else ""
        return _page("Your connections",
                     [f"Signed in as {_e(email)}"] if rows else
                     ["Nothing to connect yet.", f"Signed in as {_e(email)}"], extra=table)

    @router.post(PAGE + "/{connector_id}/connect")
    def connect_from_page(connector_id: str, request: Request):
        from ..access.session import require_same_origin
        from ..connectors import ConnectorError, oauth_flow, registry

        try:
            require_same_origin(request)
            account, email = who_of(request)
        except HTTPException:
            return _page("Request refused", ["Open Your connections and try again."], 403,
                         tone="bad")
        from ..connectors import per_user

        c = registry.get(hub_dir, connector_id)
        if (not email or not account or c is None or not c.enabled or c.auth_type == "shared"
                or c.id not in per_user.allowed_ids(hub_dir, email, [c.id])):
            return _page("Not available", ["Ask your administrator."], 403, tone="bad")
        user = user_of(request, account, email)
        back = f"{PAGE}?connected={quote(c.id)}"
        try:
            if c.auth_type == "none":
                oauth_flow.connect_without_auth(hub_dir, c, user)
                target = back
            else:
                target = oauth_flow.start(hub_dir, c, user, origin=oauth_flow.origin_for(request),
                                          return_to=back)
        except ConnectorError as err:
            return _page("Cannot connect", [_e(err.message)], err.status, tone="bad")
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store",
                                                                  "Referrer-Policy": "no-referrer"})

    @router.post(PAGE + "/{connector_id}/disconnect")
    def disconnect_from_page(connector_id: str, request: Request):
        from ..access.session import require_same_origin
        from ..connectors import tokens

        try:
            require_same_origin(request)
            account, email = who_of(request)
        except HTTPException:
            return _page("Request refused", ["Open Your connections and try again."], 403,
                         tone="bad")
        if account:
            tokens.disconnect(hub_dir, account, connector_id)
        return RedirectResponse(PAGE, status_code=303, headers={"Cache-Control": "no-store"})

    return router


def _button(action: str, text: str, *, primary: bool = False) -> str:
    cls = ' class="primary"' if primary else ""
    return (f'<form method="post" action="{action}"><button{cls} type="submit">'
            f"{html.escape(text)}</button></form>")


def _mine(hub_dir: Path, account: str, email: str) -> list:
    """``[(connector, status, allowed)]`` the person may use somewhere in this
    deployment (offered in an agent where they hold its grant), plus any they
    are still connected to without a grant (``allowed`` False: they can only
    remove it). Status: connected, expired, none or shared."""
    from ..connectors import per_user, registry, tokens

    conns = {c.connector_id: c for c in tokens.for_user(hub_dir, account)} if account else {}
    listed = [c for c in registry.list_all(hub_dir) if c.enabled]
    allowed = per_user.allowed_ids(hub_dir, email, [c.id for c in listed])
    out = []
    for c in listed:
        if c.id not in allowed and c.id not in conns:
            continue
        if c.auth_type == "shared":
            status = "shared"
        elif c.id in conns:
            status = "expired" if conns[c.id].status == "expired" else "connected"
        else:
            status = "none"
        out.append((c, status, c.id in allowed))
    return out
