"""Small member-facing consent and connection pages.

Who is signed in comes from ``access.session.verified_email``: the Hubzoid
session in the default mode (the local owner when sign-in is off), the Open
WebUI session in Open WebUI mode. Signing in happens on the web app's
``/auth`` page (Open WebUI's in Open WebUI mode), which returns here afterwards."""

from __future__ import annotations

import hmac
import secrets
import time
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
main{max-width:440px;margin:6vh auto 64px;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}
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
article form{margin:0}"""


def page(title, body, status=200):
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Hubzoid</title>
<style>{_STYLE}</style></head><body><header><span class="mark" role="img" aria-label="Hubzoid"><b>/</b>hubzoid</span></header>
<main id="main"><div class="card"><h1>{escape(title)}</h1>{body}</div></main></body></html>""",
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
            return page(
                "Connection expired",
                "<p>Start connecting again from your assistant.</p>",
            )
        who = await run_in_threadpool(viewer, request)
        if not who:
            return login(
                provider.public_path + "/consent?ticket=" + quote(ticket, safe="")
            )
        if not allowed(provider.hub_dir, who["email"]):
            return page(
                "No access",
                f"<p><strong>{escape(who['email'])}</strong> can’t use {escape(_agent_name(provider.hub_dir))}. "
                "Ask an administrator for access, then connect again.</p>",
                403,
            )
        client = await provider.get_client(pending["client_id"])
        if not client:
            return page(
                "Connection expired",
                "<p>Start connecting again from your assistant.</p>",
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
            body = f"""<p><strong>{escape(client.client_name or 'An assistant')}</strong> wants to use <strong>{escape(_agent_name(provider.hub_dir))}</strong> as <strong>{escape(who['email'])}</strong>{through}.</p>
<p>It can read this agent’s knowledge and run its tools with your access. It can’t give itself more.</p>
<form method="post" class="actions"><input type="hidden" name="csrf" value="{csrf}"><button name="decision" value="allow">Allow connection</button><button class="secondary" name="decision" value="deny">Cancel</button></form>
<dl><dt>Returns to</dt><dd><code>{escape(str(params['redirect_uri']))}</code></dd><dt>Lasts</dt><dd>Up to 30 days. Revoke any time.</dd></dl>
<p class="muted" style="margin:14px 0 0">The assistant names itself; Hubzoid can’t verify that name. Allow only if you started this.</p>"""
            response = page("Connect your assistant", body)
            response.headers.update(consent_headers)
            return set_cookie(response, csrf)
        form = await request.form()
        with store.engine.begin() as c:
            if not valid_csrf(
                request, form, who, "consent:" + ticket, c
            ) or not store.consume(c, ticket, "pending"):
                return page(
                    "Couldn’t confirm",
                    "<p>This approval expired or was already used. Start connecting again from your assistant.</p>",
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
                    return page("Couldn’t confirm", "<p>Reload the page and try again.</p>", 403)
                grant = str(form.get("grant", ""))
                g = store.get(c, grant, "grant")
                if (
                    not g
                    or g["account_id"] != who["account_id"]
                    or g["email"] != who["email"]
                ):
                    return page("Connection not found", "<p>It may already be revoked.</p>", 404)
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
        for g in grants:
            body += f'<article><strong>{escape(g["client_name"])}</strong><form method="post"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="grant" value="{escape(g["id"])}"><button class="secondary">Revoke connection</button></form></article>'
        if not grants:
            body += "<p class='muted'>No assistants are connected.</p>"
        return set_cookie(page("Your assistant connections", body), csrf)

    return [
        Route("/mcp/oauth/consent", consent, methods=["GET", "POST"]),
        Route("/mcp/oauth/connections", connections, methods=["GET", "POST"]),
    ]
