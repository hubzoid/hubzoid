"""Small member-facing consent and connection pages.

Who is signed in comes from ``access.session.verified_email``: the Hubzoid
session in the default mode (the local owner when sign-in is off), the Open
WebUI session in Open WebUI mode. Signing in happens on the web app's
``/auth`` page (Open WebUI's in Open WebUI mode), which returns here afterwards."""

from __future__ import annotations

import hmac
import secrets
import time
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote, urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool

from . import appmode
from .access import session
from .mcp_oauth_store import digest

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


# The web app's own tokens (portal/src/app/app.css), light and dark, so these
# server pages read as part of the same product.
_STYLE = """:root{--bg:#fafaf8;--card:#fff;--ink:#0b0b0c;--body:#1f1f22;--mute:#6b6b70;--line:#e7e6e2;
--accent:#b5471f;--accent-hover:#9a3b18;--accent-ink:#fff;--brand:#e5572a;--soft:#f4f3ef;color-scheme:light dark}
@media(prefers-color-scheme:dark){:root{--bg:#1b1b1d;--card:#222224;--ink:#fafaf8;--body:#e9e9e6;--mute:#b5b5bc;
--line:#3c3c40;--accent:#e5572a;--accent-hover:#f26b40;--accent-ink:#0b0b0c;--soft:#26262a}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--body);
font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
header{padding:16px 20px}@media(min-width:640px){header{padding:18px 32px}}
.mark{font:700 17px/1 "JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:-.02em;color:var(--ink)}
.mark b{color:var(--brand)}
main{max-width:460px;margin:7vh auto 64px;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:32px 32px 26px;
box-shadow:0 1px 2px rgb(0 0 0/.04),0 8px 24px rgb(0 0 0/.04)}
@media(max-width:520px){.card{border:0;box-shadow:none;background:transparent;padding:8px 4px}}
h1{margin:0 0 10px;font-size:22px;line-height:1.25;letter-spacing:-.01em;color:var(--ink);font-weight:600}
p{margin:0 0 12px}strong{color:var(--ink);font-weight:600}
.muted{color:var(--mute);font-size:13px}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);padding:1px 5px;border-radius:5px;overflow-wrap:anywhere}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 0}
button{font:inherit;font-weight:500;padding:9px 16px;border-radius:9px;border:1px solid var(--accent);
background:var(--accent);color:var(--accent-ink);cursor:pointer}
button:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
button.secondary{background:transparent;color:var(--ink);border-color:var(--line)}
button.secondary:hover{background:var(--soft)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
dl{margin:20px 0 0;padding-top:16px;border-top:1px solid var(--line);display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:13px}
dt{color:var(--mute)}dd{margin:0;overflow-wrap:anywhere}
article{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
article form{margin:0}
.parties{display:flex;align-items:center;justify-content:center;gap:10px;margin:4px 0 22px}
.tile{width:52px;height:52px;border-radius:14px;display:flex;align-items:center;justify-content:center;
font:600 18px/1 "JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);color:var(--ink);border:1px solid var(--line)}
.tile.hub b{color:var(--brand)}
.link{display:flex;gap:4px;color:var(--mute)}.link i{width:4px;height:4px;border-radius:50%;background:currentColor;display:block}
.consent{text-align:center}.consent h1{font-size:21px;margin-bottom:6px}
.who{color:var(--mute);font-size:13.5px;margin:0 0 22px}
.scope{text-align:left;margin:0 0 4px;padding:0;list-style:none}
.scope li{display:flex;gap:10px;align-items:flex-start;padding:9px 0}
.scope li+li{border-top:1px solid var(--line)}
.scope svg{flex:none;margin-top:3px}
.scope .yes{color:var(--brand)}.scope .no{color:var(--mute)}
.scope small{display:block;color:var(--mute);font-size:12.5px}
.label{text-align:left;font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--mute);margin:18px 0 2px}
.consent .actions{justify-content:center;margin-top:24px}
.consent .actions button{min-width:140px}
.fine{margin:22px 0 0;font-size:12.5px;color:var(--mute);line-height:1.5}
.fine a{color:inherit}
a.mark{text-decoration:none}:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.button{display:inline-block;padding:9px 16px;border:1px solid var(--line);border-radius:9px;color:var(--ink);text-decoration:none}
.help{display:inline-block;position:relative;margin-left:4px;vertical-align:middle}
.help button{width:24px;height:24px;padding:0;border-color:var(--line);border-radius:50%;background:transparent;color:var(--mute);font:600 12px system-ui}
.tooltip{display:none;position:absolute;z-index:1;top:calc(100% + 6px);right:0;width:250px;
padding:12px;background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--ink);font-size:13px;text-align:left}
.help:hover .tooltip,.help:focus-within .tooltip{display:block}
details{margin-top:16px;text-align:left;font-size:13px}summary{cursor:pointer;color:var(--mute)}
article p{margin:4px 0 0}
@media(max-width:600px){.tooltip{position:fixed;top:auto;bottom:24px;left:24px;right:24px;width:auto}}"""


def page(title, body, status=200, *, own_heading=False):
    heading = "" if own_heading else f"<h1>{escape(title)}</h1>"
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Hubzoid</title>
<style>{_STYLE}</style></head><body><header><a class="mark" href="/" aria-label="Hubzoid home"><b>/</b>hubzoid</a></header>
<main id="main"><div class="card">{heading}{body}</div></main></body></html>""",
        status_code=status,
        headers=HEADERS,
    )


def _agent_name(hub_dir) -> str:
    """The agent's own name for people, else its folder."""
    try:
        from .loaders import agents

        return agents.load_main(hub_dir).spec.name or hub_dir.name
    except Exception:  # noqa: BLE001 — a page still renders with the folder name
        return hub_dir.name


def _info(label, text, identifier):
    return (f'<span class="help"><button type="button" aria-label="{escape(label)}" '
            f'aria-describedby="{identifier}">?</button><span class="tooltip" '
            f'id="{identifier}" role="tooltip">{escape(text)}</span></span>')


def _notice(title, message, status=200, *, path="/", action="Back to Hubzoid"):
    return page(title, f'<p>{escape(message)}</p><p><a class="button" '
                f'href="{escape(path)}">{escape(action)}</a></p>', status)


def _date(timestamp):
    value = datetime.fromtimestamp(timestamp, timezone.utc)
    return f'<time datetime="{value.isoformat()}">{value:%d %b %Y} UTC</time>'


def browser_routes(provider):
    from .mcp_oauth import account, allowed, SCOPE, GRANT_SECONDS

    store = provider.store
    cookie_name = "hz_mcp_" + digest(provider.resource)[:12]

    def viewer(request):
        email = session.verified_email(request, provider.hub_dir)
        return account(provider.hub_dir, email=email) if email else None

    def login(path):
        # Fixed local return URL; no client-supplied redirect becomes a sign-in target.
        return RedirectResponse(
            "/auth?redirect=" + quote(path, safe=""), status_code=303, headers=HEADERS
        )

    def csrf_form(who, purpose):
        csrf = secrets.token_urlsafe(32)
        with store.engine.begin() as c:
            store.cleanup(c)
            store.put(
                c, csrf, "browser", {**who, "purpose": purpose}, time.time() + 600
            )
        return csrf

    def set_cookie(response, csrf):
        response.set_cookie(
            cookie_name,
            csrf,
            max_age=600,
            httponly=True,
            secure=provider.origin.startswith("https:"),
            samesite="lax",
            path=provider.public_path,
        )
        return response

    def valid_csrf(request, form, who, purpose, c):
        value = str(form.get("csrf", ""))
        if (
            request.headers.get("origin") != provider.origin
            or not value
            or not hmac.compare_digest(value, request.cookies.get(cookie_name, ""))
        ):
            return False
        d = store.get(c, value, "browser")
        return bool(
            d
            and not d["_used"]
            and d["account_id"] == who["account_id"]
            and d["email"] == who["email"]
            and d["purpose"] == purpose
            and store.consume(c, value, "browser")
        )

    async def consent(request):
        ticket = request.query_params.get("ticket", "")
        with store.engine.connect() as c:
            pending = store.get(c, ticket, "pending")
        if not pending or pending["_used"]:
            return _notice(
                "Connection expired",
                "Start connecting again from your assistant.",
            )
        who = await run_in_threadpool(viewer, request)
        if not who:
            return login(
                provider.public_path + "/consent?ticket=" + quote(ticket, safe="")
            )
        if not allowed(provider.hub_dir, who["email"]):
            return _notice(
                "Hub access needed",
                f"{who['email']} does not have access to {_agent_name(provider.hub_dir)}. "
                "Ask an administrator for access, then reconnect from your assistant.",
                403,
            )
        client = await provider.get_client(pending["client_id"])
        if not client:
            return _notice(
                "Connection expired",
                "Start connecting again from your assistant.",
                400,
            )
        params = pending["params"]
        callback = urlsplit(str(params["redirect_uri"]))
        # Registration validates the callback authority. Chromium applies
        # form-action to POST redirect chains as well as the form target.
        callback_origin = f"{callback.scheme}://{callback.netloc}"
        consent_headers = {
            **HEADERS,
            "Content-Security-Policy": HEADERS["Content-Security-Policy"].replace(
                "form-action 'self'", f"form-action 'self' {callback_origin}"
            ),
        }
        if request.method == "GET":
            through = " through Open WebUI" if appmode.is_openwebui(provider.hub_dir) else ""
            csrf = csrf_form(who, "consent:" + ticket)
            app_name = client.client_name or "An assistant"
            agent = _agent_name(provider.hub_dir)
            back = urlsplit(str(params["redirect_uri"]))
            yes = '<svg class="yes" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
            no = '<svg class="no" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
            name_help = _info("About the app name", "The client supplies this name. Hubzoid does not verify it; check the return address before connecting.", "client-help")
            body = f"""<div class="consent">
<div class="parties" aria-hidden="true"><span class="tile">{escape(app_name[:1].upper())}</span><span class="link"><i></i><i></i><i></i></span><span class="tile hub"><b>/</b>{escape(agent[:1].lower())}</span></div>
<h1>{escape(app_name)} wants to use {escape(agent)}</h1>
<p class="who">Signed in as <strong>{escape(who['email'])}</strong>{through}</p>
<p class="label">This will allow it to</p>
<ul class="scope">
<li>{yes}<span>Read {escape(agent)}’s knowledge and files</span></li>
<li>{yes}<span>Use {escape(agent)}’s tools, including ones that make changes<small>Only the tools your access allows, as you.</small></span></li>
</ul>
<p class="label">It can’t</p>
<ul class="scope">
<li>{no}<span>Do anything your access doesn’t allow</span></li>
<li>{no}<span>Use other agents, or keep access after you revoke it</span></li>
</ul>
<form method="post" class="actions"><input type="hidden" name="csrf" value="{csrf}"><button class="secondary" name="decision" value="deny">Cancel</button><button name="decision" value="allow">Allow connection</button></form>
<p class="fine">Access lasts up to 30 days. You can revoke it any time in <a href="{escape(provider.public_path)}/connections">your connections</a>.<br>
Returns to <code>{escape(back.netloc or str(params['redirect_uri']))}</code>. The assistant names itself, so allow this only if you started it.{name_help}</p>
<details><summary>Connection details</summary><dl><dt>MCP server</dt><dd><code>{escape(provider.resource)}</code></dd>
<dt>Return address</dt><dd><code>{escape(str(params['redirect_uri']))}</code></dd></dl></details>
</div>"""
            response = page(f"Connect {app_name}", body, own_heading=True)
            response.headers.update(consent_headers)
            return set_cookie(response, csrf)
        form = await request.form()
        with store.engine.begin() as c:
            if not valid_csrf(
                request, form, who, "consent:" + ticket, c
            ) or not store.consume(c, ticket, "pending"):
                return _notice(
                    "Couldn’t confirm",
                    "This approval expired or was already used. Start connecting again from your assistant.",
                    403,
                )
            decision = form.get("decision")
            if decision != "allow":
                url = construct_redirect_uri(
                    str(params["redirect_uri"]),
                    error="access_denied",
                    state=params.get("state"),
                )
            else:
                grant, code = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                store.put(
                    c,
                    grant,
                    "grant",
                    {
                        **who,
                        "id": grant,
                        "client_id": client.client_id,
                        "client_name": client.client_name or "MCP assistant",
                        "created": int(time.time()),
                    },
                    time.time() + GRANT_SECONDS,
                )
                store.put(
                    c,
                    code,
                    "code",
                    {
                        "grant": grant,
                        "client_id": client.client_id,
                        "scopes": [SCOPE],
                        "expires_at": int(time.time()) + 60,
                        "code_challenge": params["code_challenge"],
                        "redirect_uri": params["redirect_uri"],
                        "redirect_uri_provided_explicitly": params[
                            "redirect_uri_provided_explicitly"
                        ],
                        "resource": provider.resource,
                        "subject": who["account_id"],
                    },
                    time.time() + 60,
                )
                url = construct_redirect_uri(
                    str(params["redirect_uri"]), code=code, state=params.get("state")
                )
        response = RedirectResponse(url, status_code=303, headers=consent_headers)
        response.delete_cookie(cookie_name, path=provider.public_path)
        return response

    async def connections(request):
        who = await run_in_threadpool(viewer, request)
        if not who:
            return login(provider.public_path + "/connections")
        if request.method == "POST":
            form = await request.form()
            with store.engine.begin() as c:
                if not valid_csrf(request, form, who, "connections", c):
                    return _notice("Couldn’t confirm", "Reload the page and try again.", 403,
                                   path=provider.public_path + "/connections", action="Reload connections")
                grant = str(form.get("grant", ""))
                g = store.get(c, grant, "grant")
                if (
                    not g
                    or g["account_id"] != who["account_id"]
                    or g["email"] != who["email"]
                ):
                    return _notice("Connection not found", "It may already be revoked.", 404,
                                   path=provider.public_path + "/connections", action="Reload connections")
                store.revoke(c, grant)
            return RedirectResponse(
                provider.public_path + "/connections", status_code=303, headers=HEADERS
            )
        csrf = csrf_form(who, "connections")
        with store.engine.connect() as c:
            grants = [
                g
                for g in store.grants(c)
                if g["account_id"] == who["account_id"] and g["email"] == who["email"]
            ]
        body = (f'<p>Assistants using <strong>{escape(_agent_name(provider.hub_dir))}</strong> as '
                f'<strong>{escape(who["email"])}</strong>, with your current access.</p>')
        body += f'<p class="muted">Revoke a connection to stop future access.{_info("About revoking access", "Revoking does not undo actions or remove data already shared with the assistant.", "revoke-help")}</p>'
        for g in sorted(grants, key=lambda g: g["created"], reverse=True):
            body += f'<article><div><strong>{escape(g["client_name"])}</strong><p class="muted">Connected {_date(g["created"])} · Expires {_date(g["_expires"])}</p></div><form method="post"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="grant" value="{escape(g["id"])}"><button class="secondary">Revoke connection</button></form></article>'
        if not grants:
            body += "<p class='muted'>No assistants are connected. Start a connection from your assistant’s MCP settings.</p>"
        return set_cookie(page("Your assistant connections", body), csrf)

    return [
        Route("/mcp/oauth/consent", consent, methods=["GET", "POST"]),
        Route("/mcp/oauth/connections", connections, methods=["GET", "POST"]),
    ]
