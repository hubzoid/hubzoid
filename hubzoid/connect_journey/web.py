"""The connection pages: `/portal/connect/<id>` and its actions. A person sees
and manages their connections in the web app's Settings (`/account/connections`);
the older `/portal/connections` address sends them there.

Every link page is bound to the journey's account: it needs a signed-in
session whose account id and email are the ones that asked, checked
server-side: a Hubzoid session (`hubzoid.auth.current_user`). A signed-out visitor is sent to sign in and brought back to
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
import re
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..access.identity import normalize
from . import store
from . import pages as ui
from .providers import JourneyError, label

log = logging.getLogger("hubzoid.connect")

POLL_SECONDS = 30
PAGE = "/account/connections"
OLD_PAGE = "/portal/connections"


def _headers(nonce: str | None = None) -> dict:
    return ui.headers(nonce)


_TILES = {"ok": ("ok", "check"), "bad": ("bad", "alert"), "warn": ("warn", "alert"),
          "lock": ("", "lock")}


def _page(title: str, paragraphs: list[str], status: int = 200, *, tone: str = "",
          actions: str = "", script: str = "", eyebrow: str = "",
          extra: str = "", brand: str = "") -> HTMLResponse:
    """A message page: an icon for the tone, the title, a lead line and any
    further lines (HTML the caller escaped), and its actions."""
    tile = _TILES.get(tone)
    head = (f'<div class="pair"><span class="tile {tile[0]}">{ui.icon(tile[1], 22)}</span></div>'
            if tile else "")
    body = "".join(f'<p class="{"lead" if i == 0 else "muted"}">{p}</p>'
                   for i, p in enumerate(paragraphs))
    card = (f'<section class="card">{head}'
            + (f'<p class="eyebrow">{_e(eyebrow)}</p>' if eyebrow else "")
            + f"<h1>{_e(title)}</h1>{body}{extra}"
            + (f'<div class="actions">{actions}</div>' if actions else "")
            + "</section>")
    return ui.shell(title, card, status=status, brand=brand, script=script)


def _e(value) -> str:
    return html.escape(str(value or ""))


def _not_found() -> HTMLResponse:
    return _page("Link not found", ["Ask the agent for a new link."], 404, tone="bad")


def _gone(j: dict) -> HTMLResponse:
    back, script = _back(j)
    if j["status"] == "superseded":
        return _page("A newer link was sent", ["Use the newest link the agent sent."], 410,
                     tone="bad", actions=back, script=script)
    return _page("This link has expired", ["Ask the agent for a new link."], 410, tone="bad",
                 actions=back, script=script)


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# This tab was opened from the chat: close it when the browser allows (the
# chat's own tab is still there), else open the chat here.
_BACK_SCRIPT = ("(function(){var a=document.getElementById('back');if(!a)return;"
                "a.addEventListener('click',function(e){e.preventDefault();var h=a.href;"
                "try{window.close()}catch(x){}setTimeout(function(){location.href=h},250)})})();")


def _chat_url(j: dict) -> str | None:
    """Where the person asked, on this site: the conversation (``/c/<id>``,
    also for a journey started in 1.x chat), or the chat's home when the id
    is not a conversation's. None for WhatsApp, Telegram or Slack."""
    if j.get("surface") not in ("web", "owui"):
        return None
    from ..chat.store import WEB_PREFIX, valid_conversation_id

    cid = str(j.get("chat_id") or "")
    if cid.startswith(WEB_PREFIX) and valid_conversation_id(cid[len(WEB_PREFIX):]):
        return "/c/" + cid[len(WEB_PREFIX):]
    if _UUID.match(cid):
        return "/c/" + cid
    return "/"


def _back(j: dict, *, primary: bool = True) -> tuple[str, str]:
    """The Back to chat button for a journey and its script, or ("", "")."""
    url = _chat_url(j)
    if url is None:
        return "", ""
    kind = "primary" if primary else "secondary"
    return (f'<a class="btn btn-{kind}" id="back" href="{_e(url)}">{ui.icon("back", 16)}Back to chat</a>',
            _BACK_SCRIPT)


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
    """The web app's sign-in, returning to this journey afterwards. Built only
    from a journey id that exists, never from the request: no open redirect."""
    return "/auth?redirect=" + quote(f"/portal/connect/{jid}{page}", safe="/")


def _sign_in(jid: str) -> HTMLResponse:
    return _page("Sign in to continue",
                 ["This link is personal. Sign in with the account that asked for it.",
                  "You'll come back here after signing in."], 401, tone="lock",
                 actions=f'<a class="btn btn-primary" href="{_sign_in_url(jid)}">Sign in</a>')


def _redirect_to_sign_in(jid: str, page: str = "") -> RedirectResponse:
    return RedirectResponse(_sign_in_url(jid, page), status_code=302,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"})


def _app_name(hub_dir: Path, app: str) -> str:
    """The connector's display name, as the Console shows it."""
    from ..connectors import registry

    try:
        c = registry.get(Path(hub_dir), app)
    except Exception:  # noqa: BLE001 — wording only
        c = None
    return c.name if c is not None else label(app)


def _outcome(hub_dir: Path, j: dict, email: str) -> HTMLResponse:
    name = _app_name(hub_dir, j["app"])
    app = _e(name)
    agent = _agent_name(hub_dir, j["hub"])
    status = j["status"]
    button, script = _back(j)
    if j.get("surface") == "whatsapp":
        lead = "You'll get a confirmation in WhatsApp."
    elif button:
        lead = "You can carry on in your chat."
    else:
        lead = "Close this tab and carry on in your chat."
    if status == "connected":
        details = ui.rows([("Account", ui.avatar_chip(email))])
        return _page(f"{name} connected", [lead], 200, tone="ok",
                     brand=agent, extra=details, script=script,
                     actions=button + f'<a class="btn btn-secondary" href="{PAGE}">Connections</a>')
    if status == "failed":
        return _page(f"{name} was not connected", ["Ask the agent for a new link to try again."],
                     200, tone="bad", brand=agent, actions=button, script=script)
    if status == "cancelled":
        return _page("Connection cancelled",
                     [f"{app} was not connected. Ask the agent again when you're ready."], 200,
                     tone="warn", brand=agent, actions=button, script=script)
    return _gone(j)


def _connect_page(hub_dir: Path, j: dict, email: str) -> HTMLResponse:
    """What is about to happen, before anything does: who connects what, for
    which agent, at which server, with which access (Agno's consent page lists
    the scopes and where credentials go; Sim's lists the scopes)."""
    from urllib.parse import urlsplit

    from ..connectors import registry

    agent = _agent_name(hub_dir, j["hub"])
    c = registry.get(hub_dir, j["app"])
    name = c.name if c is not None else label(j["app"])
    base = f"/portal/connect/{quote(j['id'])}"
    details = [("Account", ui.avatar_chip(email))]
    if c is not None:
        details.append(("Connects to", _e(urlsplit(c.url).hostname or c.url)))
    access = [f"Access requested: {_e(c.scopes)}."] if c is not None and c.scopes else []
    pair = (f'<div class="pair"><span class="tile agent" aria-hidden="true"><b style="color:var(--brand)">/</b>'
            f'{_e(agent[:1].lower())}</span><span class="link">{ui.icon("link", 14)}</span>'
            f'<span class="tile app" aria-hidden="true">{ui.initial(name)}</span></div>')
    card = (f'<section class="card">{pair}'
            f"<h1>Connect {_e(name)}</h1>"
            f'<p class="lead">{_e(agent)} will use {_e(name)} as you.</p>'
            + ui.rows(details)
            + ui.help_text("How this works", [
                f"You approve access on {_e(name)}'s own page. Hubzoid never sees your password.",
                *access,
                "Hubzoid keeps it encrypted, and agents use it only for you. Disconnect any "
                f"time in <a href=\"{PAGE}\">Settings, My connections</a>."])
            + f'<div class="actions"><form method="post" action="{base}/start">'
              f'<button class="btn btn-primary" type="submit">Continue to {_e(name)} '
              f'{ui.icon("arrow", 16)}</button></form>'
              f'<form method="post" action="{base}/cancel">'
              f'<button class="btn btn-ghost" type="submit">Cancel</button></form></div>'
            + "</section>")
    return ui.shell(f"Connect {name}", card, brand=agent)


def build_router(hub_dir: Path, *, session_email=None) -> APIRouter:
    router = APIRouter()
    hub_dir = Path(hub_dir)

    def who_of(request: Request) -> tuple[str, str]:
        """``(account id, email)`` of the signed-in viewer, or ``("", "")``."""
        if session_email is not None:
            email = normalize(session_email(request) or "")
            if not email:
                return "", ""
            from ..connectors import tokens

            return tokens.user_id_for(hub_dir, email) or "", email
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
                               [f"You're signed in as {_e(email)}.",
                                "Sign in with the account that asked for it, or ask the agent "
                                "from your own chat."], 403, tone="bad")
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
            return _outcome(hub_dir, j, email)
        return _connect_page(hub_dir, j, email)

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
            return _outcome(hub_dir, j, email)
        if not still_permitted(j):
            store.audit(hub_dir, hub=j["hub"], subject=email, surface="web", app=j["app"],
                        decision="deny", reason="no longer permitted")
            return _page("Not permitted",
                         [f"You can no longer connect {_e(_app_name(hub_dir, j['app']))}.",
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
        return _outcome(hub_dir, store.get(hub_dir, jid) or j, email)

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
            app = _e(_app_name(hub_dir, j["app"]))
            status_url = f"/portal/connect/{quote(jid)}/status"
            script = (
                "(function(){var t0=Date.now();function tick(){fetch(" + repr(status_url)
                + ",{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.json()})"
                ".then(function(d){if(d.state&&d.state!=='pending'&&d.state!=='started'){"
                "location.reload();return}next()}).catch(next)}"
                "function next(){if(Date.now()-t0<" + str(POLL_SECONDS * 1000) + "){"
                "setTimeout(tick,2000)}else{var m=document.getElementById('wait'),s=document.getElementById('spin');"
                "if(s){s.remove()}if(m){m.textContent='This is taking longer than expected. If you "
                "closed the sign-in window, ask the agent for a new link.'}}}"
                "setTimeout(tick,1500)})();")
            waiting = (f'<div class="waiting"><span class="spin" id="spin" aria-hidden="true"></span>'
                       f'<span id="wait">Checking your {app} connection…</span></div>'
                       '<p class="muted">This page updates by itself.</p>')
            return _page("Finishing up", [], 202, extra=waiting,
                         brand=_agent_name(hub_dir, j["hub"]), script=script)
        return _outcome(hub_dir, j, email)

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

    # ---- The older connections address ------------------------------------------
    @router.get(OLD_PAGE)
    def connections(request: Request):
        """An older address for a person's connections. It stays, because links
        in chat history and sign-ins under way return here, and carries its
        query (``connected``, ``connector``, ``error``) into Settings."""
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(PAGE + query, status_code=302,
                                headers={"Cache-Control": "no-store"})

    return router
