"""Small member-facing consent and connection pages; login stays in Open WebUI."""

from __future__ import annotations

import hmac
import secrets
import time
from html import escape
from urllib.parse import quote, urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from starlette.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool

from .access import session
from .mcp_oauth_store import digest

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def page(title, body):
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Hubzoid</title>
<style>body{{margin:0;background:#f5f4ef;color:#192c2b;font:17px/1.55 system-ui,sans-serif}}main{{max-width:560px;margin:8vh auto;padding:28px;background:white;border:1px solid #ddd;border-radius:20px}}h1{{line-height:1.15}}p,code{{overflow-wrap:anywhere}}.brand{{font-weight:750;color:#27685a}}button{{padding:12px 20px;margin:8px 8px 0 0;border:1px solid #27685a;border-radius:9px;background:#27685a;color:white;font:inherit;cursor:pointer}}button.secondary{{background:white;color:#27685a}}article{{border-top:1px solid #ddd;padding:18px 0}}small{{color:#596565}}@media(max-width:640px){{main{{margin:20px 12px;padding:22px}}}}</style><main><div class="brand">Hubzoid</div><h1>{escape(title)}</h1>{body}</main></html>""",
        headers=HEADERS,
    )


def browser_routes(provider):
    from .mcp_oauth import account, allowed, SCOPE, GRANT_SECONDS

    store = provider.store
    cookie_name = "hz_mcp_" + digest(provider.resource)[:12]

    def viewer(request):
        email = session.verified_email(request, provider.hub_dir)
        return account(provider.hub_dir, email=email) if email else None

    def login(path):
        # Fixed local return URL; no client-supplied redirect becomes an OWUI target.
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
        if not allowed(provider.hub_dir, who["email"], provider.access_group):
            return PlainTextResponse(
                "Your account does not have access to this hub.", 403, headers=HEADERS
            )
        client = await provider.get_client(pending["client_id"])
        if not client:
            return PlainTextResponse(
                "Client registration expired. Reconnect from your assistant.",
                400,
                headers=HEADERS,
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
            csrf = csrf_form(who, "consent:" + ticket)
            body = f"""<p><strong>{escape(client.client_name or 'MCP assistant')}</strong> wants to connect to <strong>{escape(provider.hub_dir.name)}</strong>.</p>
<p>Signed in as <strong>{escape(who['email'])}</strong> through Open WebUI.</p><p>This allows the assistant to read hub context and run tools, including actions, using your current hub permissions. It cannot grant itself additional permissions.</p>
<p><small>The application name is supplied by the client and is not verified. Continue only if you started this connection.</small></p><p>Return address: <code>{escape(str(params['redirect_uri']))}</code></p><p>Connection lasts up to 30 days. You can revoke it at any time.</p>
<form method="post"><input type="hidden" name="csrf" value="{csrf}"><button name="decision" value="allow">Allow connection</button><button class="secondary" name="decision" value="deny">Cancel</button></form>"""
            response = page("Connect your assistant", body)
            response.headers.update(consent_headers)
            return set_cookie(response, csrf)
        form = await request.form()
        with store.engine.begin() as c:
            if not valid_csrf(
                request, form, who, "consent:" + ticket, c
            ) or not store.consume(c, ticket, "pending"):
                return PlainTextResponse(
                    "Invalid or expired approval. Start again.", 403, headers=HEADERS
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
                    return PlainTextResponse(
                        "Invalid or expired request. Reload the page.",
                        403,
                        headers=HEADERS,
                    )
                grant = str(form.get("grant", ""))
                g = store.get(c, grant, "grant")
                if (
                    not g
                    or g["account_id"] != who["account_id"]
                    or g["email"] != who["email"]
                ):
                    return PlainTextResponse(
                        "Connection not found.", 404, headers=HEADERS
                    )
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
        body = f'<p>Signed in as {escape(who["email"])}. These connections can use your current permissions in {escape(provider.hub_dir.name)}.</p>'
        for g in grants:
            body += f'<article><strong>{escape(g["client_name"])}</strong><form method="post"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="grant" value="{escape(g["id"])}"><button class="secondary">Revoke connection</button></form></article>'
        if not grants:
            body += "<p>No active assistant connections.</p>"
        body += "<p><small>These connections use OAuth. Static API keys cannot connect to this MCP server.</small></p>"
        return set_cookie(page("Your assistant connections", body), csrf)

    return [
        Route("/mcp/oauth/consent", consent, methods=["GET", "POST"]),
        Route("/mcp/oauth/connections", connections, methods=["GET", "POST"]),
    ]
