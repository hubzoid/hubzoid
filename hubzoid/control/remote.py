"""The remote CLI client: run commands against a hosted Hubzoid.

``--server URL`` (or ``HUBZOID_SERVER``) picks the deployment. The credential
is, in order:

  * ``HUBZOID_API_KEY``: a personal API key (``hzp_...``), for scripts and CI;
  * the session saved by ``hubzoid auth login --server URL`` (password, then a
    code when two-step sign-in is on). Only the session token is saved, in
    ``~/.config/hubzoid/credentials.json`` with owner-only permissions: never
    the password or an authenticator secret. ``hubzoid auth logout`` signs it
    out on the server and removes it.

Each command calls one API route and prints exactly what it returns (with
``--json``). A remote failure never falls back to local files. Exit codes are
the control plane's (``control.cli_common``): 0 done, 1 failed, 2 invalid
input, 3 not allowed (401/403), 4 conflict (409/410), 5 unavailable (5xx,
timeout), 6 accepted but not finished (202).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import urlparse

import httpx

SESSION_COOKIE = "hz_session"
TIMEOUT = 30.0


class RemoteError(Exception):
    def __init__(self, code: int, message: str, body=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.body = body


def credentials_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "hubzoid" / "credentials.json"


def _read_all() -> dict:
    path = credentials_path()
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_all(data: dict) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)


def normalize_server(server: str) -> str:
    server = (server or "").strip().rstrip("/")
    parsed = urlparse(server)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RemoteError(2, "Give the server as a URL, for example https://team.example.com.")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise RemoteError(2, "Use https for a server that isn't on this computer.")
    return f"{parsed.scheme}://{parsed.netloc}"


def server_from(option: str | None) -> str | None:
    value = option or os.environ.get("HUBZOID_SERVER") or ""
    return normalize_server(value) if value else None


def save_session(server: str, token: str, email: str) -> None:
    data = _read_all()
    data[server] = {"session": token, "email": email}
    _write_all(data)


def saved(server: str) -> dict | None:
    return _read_all().get(server)


def forget(server: str) -> None:
    data = _read_all()
    if data.pop(server, None) is not None:
        _write_all(data)


def exit_code(status: int) -> int:
    if status in (401, 403):
        return 3
    if status in (409, 410):
        return 4
    if status in (400, 422):
        return 2
    if status >= 500:
        return 5
    if status == 202:
        return 6
    return 0 if status < 400 else 1


class Client:
    """One deployment, one credential."""

    def __init__(self, server: str, *, api_key: str | None = None, session: str | None = None):
        self.server = normalize_server(server)
        self.api_key = api_key if api_key is not None else (os.environ.get("HUBZOID_API_KEY") or None)
        found = saved(self.server) if session is None and not self.api_key else None
        self.session = session or ((found or {}).get("session"))
        self.http = httpx.Client(base_url=self.server, timeout=TIMEOUT, follow_redirects=False)

    def headers(self) -> dict:
        h = {"accept": "application/json", "user-agent": "hubzoid-cli"}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        else:
            h["origin"] = self.server  # the same-origin rule for a session
        return h

    def request(self, method: str, path: str, body=None, *, cookies: dict | None = None) -> httpx.Response:
        jar = dict(cookies or {})
        if self.session and not self.api_key:
            jar.setdefault(SESSION_COOKIE, self.session)
        try:
            return self.http.request(method.upper(), path, headers=self.headers(),
                                     json=body, cookies=jar or None)
        except httpx.TimeoutException as exc:
            raise RemoteError(5, f"{self.server} did not answer in time.") from exc
        except httpx.HTTPError as exc:
            raise RemoteError(5, f"Could not reach {self.server}: {type(exc).__name__}.") from exc

    def call(self, method: str, path: str, body=None):
        """The route's JSON (or None), raising RemoteError with the exit code."""
        r = self.request(method, path, body)
        try:
            data = r.json() if r.content else None
        except ValueError:
            data = {"text": r.text[:2000]}
        if r.status_code >= 400:
            detail = (data or {}).get("detail") if isinstance(data, dict) else None
            message = (detail.get("message") if isinstance(detail, dict) else detail) or \
                f"The server answered HTTP {r.status_code}."
            raise RemoteError(exit_code(r.status_code), str(message), data)
        return data
