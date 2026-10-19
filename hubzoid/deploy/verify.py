"""`hubzoid deploy verify URL`: a hosted hub checked from outside, as a
person's browser and an MCP client meet it. Like eve's doctor: one line per
check, and a failure exits 1."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit


@dataclass
class Result:
    id: str
    status: str          # ok | warn | fail
    summary: str
    fix: str = ""

    def public(self) -> dict:
        return asdict(self)


def origin(url: str) -> str:
    """The deployment's origin: scheme and host, no credentials, path or query."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{url!r} is not an http(s) address.")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError(f"{url!r}: give the address only, with no login, path or query.")
    return f"{parts.scheme}://{parts.netloc}"


def _get(client, url: str, attempts: int):
    """GET with a few tries: a sleeping service answers its first request late."""
    import httpx

    last = None
    for n in range(attempts):
        try:
            r = client.get(url)
            if r.status_code not in (502, 503, 504) or n == attempts - 1:
                return r, None
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(min(2 ** n, 5))
    return None, last


def _public_url(session: dict, base: str) -> Result:
    configured = session.get("public_url")
    if "public_url" not in session:
        return Result("public_url", "warn", "This release does not report its public address.",
                      f"Check HUBZOID_PUBLIC_URL by hand: it must be exactly {base}.")
    if not configured:
        return Result("public_url", "fail", "HUBZOID_PUBLIC_URL is not set.",
                      f"Set HUBZOID_PUBLIC_URL={base}: sign-in, MCP and connector callbacks use it.")
    if configured.rstrip("/") != base:
        return Result("public_url", "fail", f"HUBZOID_PUBLIC_URL is {configured}, not this address.",
                      f"Set HUBZOID_PUBLIC_URL={base}.")
    return Result("public_url", "ok", "HUBZOID_PUBLIC_URL is this address.")


def run(url: str, *, allow_http: bool = False, timeout: float = 15.0, attempts: int = 4, client=None) -> list[Result]:
    import httpx

    base = origin(url)
    out: list[Result] = []
    if base.startswith("https://"):
        out.append(Result("https", "ok", "The address uses HTTPS."))
    else:
        out.append(Result("https", "warn" if allow_http else "fail", "The address is plain HTTP.",
                          "Serve the hub over HTTPS (the provider's domain does) and use that address."))
    own = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=False,
                                    headers={"User-Agent": "hubzoid-deploy-verify"})
    try:
        r, error = _get(client, base + "/healthz", attempts)
        if r is None:
            reason = "its TLS certificate is not valid" if "SSL" in str(error) or "certificate" in str(error) \
                else f"{type(error).__name__}"
            out.append(Result("health", "fail", f"No answer from {base}/healthz: {reason}.",
                              "Check the service is running and the address is right. A sleeping service "
                              "wakes on this request: try again in a minute."))
            return out
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code != 200:
            out.append(Result("health", "fail", f"/healthz answered {r.status_code}.",
                              "The hub is not ready: read the service's logs."))
        elif body != {"status": "ok"}:
            out.append(Result("health", "fail", "/healthz answers, but says more than its status.",
                              "Run a current Hubzoid release: its public health check names no hub or model."))
        else:
            out.append(Result("health", "ok", "/healthz answers {\"status\": \"ok\"} and nothing else."))

        session = None
        try:
            s = client.get(base + "/api/auth/session")
            session = s.json() if s.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            session = None
        if not isinstance(session, dict):
            out.append(Result("sign_in", "fail", "/api/auth/session did not answer as a Hubzoid web app does.",
                              "Check the address points at the hub."))
        elif session.get("mode") == "local":
            out.append(Result("sign_in", "fail", "Sign-in is off: whoever opens the address is the owner.",
                              "Set HUBZOID_AUTH=true and restart."))
        else:
            out.append(Result("sign_in", "ok", "Sign-in is on."))

        try:
            c = client.get(base + "/api/conversations")
            code = c.status_code
        except httpx.HTTPError as exc:
            code = type(exc).__name__
        if code in (401, 403):
            out.append(Result("data", "ok", "App data needs sign-in (/api/conversations refused a signed-out request)."))
        elif code == 200:
            out.append(Result("data", "fail", "/api/conversations answered a signed-out request.",
                              "Turn sign-in on (HUBZOID_AUTH=true)."))
        else:
            out.append(Result("data", "warn", f"/api/conversations answered {code}; expected 401."))

        if isinstance(session, dict):
            out.append(_public_url(session, base))
    finally:
        if own:
            client.close()
    return out
