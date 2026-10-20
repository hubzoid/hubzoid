"""GitHub credentials, held on the host only.

App mode mints an installation token narrowed to exactly one repository and
the named permissions for each call (githubkit's ``AppInstallationAuthStrategy``
with ``repository_ids`` and ``permissions``). Personal-token mode is for a
local hub. No token is ever returned by an API, put in a model's context or a
sandbox, or written to a file. A client used for a write never retries on its
own and never follows a redirect, so an uncertain write is never repeated
behind the record's back.
"""
from __future__ import annotations

import os
import time

#: tests inject an httpx transport here
transport = None

READ = {"metadata": "read", "contents": "read", "issues": "read", "pull_requests": "read", "checks": "read",
        "actions": "read"}
WRITE_ISSUES = {"metadata": "read", "issues": "write", "pull_requests": "write"}


def api_url() -> str:
    return (os.environ.get("HUBZOID_GITHUB_API_URL") or "https://api.github.com").rstrip("/")


def web_url() -> str:
    return (os.environ.get("HUBZOID_GITHUB_WEB_URL") or "https://github.com").rstrip("/")


def _gh(auth, *, writes: bool):
    from githubkit import GitHub

    kwargs = dict(base_url=api_url() + "/", auto_retry=not writes, follow_redirects=not writes, timeout=20,
                  http_cache=False)
    if transport is not None:
        kwargs["transport"] = transport
    return GitHub(auth, **kwargs)


def app_client(conn):
    """Authenticated as the App itself (JWT): installation and App metadata only."""
    from githubkit import AppAuthStrategy

    cfg = conn.settings
    return _gh(AppAuthStrategy(str(cfg.app_id), conn.secret("private_key_ref")), writes=False)


def app_client_raw(app_id, private_key: str):
    from githubkit import AppAuthStrategy

    return _gh(AppAuthStrategy(str(app_id), private_key), writes=False)


def for_repo(conn, repository_id: int, permissions: dict | None = None, *, writes: bool = False):
    """A client for one repository with only the named permissions."""
    cfg = conn.settings
    if cfg.mode == "pat":
        from githubkit import TokenAuthStrategy

        return _gh(TokenAuthStrategy(conn.secret("token_ref")), writes=writes)
    from githubkit import AppInstallationAuthStrategy

    auth = AppInstallationAuthStrategy(str(cfg.app_id), conn.secret("private_key_ref"), int(cfg.installation_id),
                                       repository_ids=[int(repository_id)],
                                       permissions=dict(permissions or READ))
    return _gh(auth, writes=writes)


def installation_client(conn):
    """The installation with its granted permissions, for listing its repositories."""
    from githubkit import AppInstallationAuthStrategy

    cfg = conn.settings
    return _gh(AppInstallationAuthStrategy(str(cfg.app_id), conn.secret("private_key_ref"),
                                           int(cfg.installation_id)), writes=False)


def pat_client_raw(token: str):
    from githubkit import TokenAuthStrategy

    return _gh(TokenAuthStrategy(token), writes=False)


def status_of(exc) -> int | None:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


_NAMES: dict[tuple, tuple[float, str]] = {}
NAME_SECONDS = 300


def current_name(conn, web, repository_id: int) -> str:
    """The repository's name now, read by its id. A renamed repository keeps
    its id; its old name can be reused by someone else, so a name from the
    file is never trusted for a call. Refuses a repository that moved to
    another owner."""
    key = (str(conn.hub_dir), conn.id, int(repository_id))
    hit = _NAMES.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    from ..errors import ChannelError

    try:
        data = web.request("GET", f"/repositories/{int(repository_id)}").json() or {}
    except Exception as exc:  # noqa: BLE001
        if status_of(exc) in (403, 404):
            raise ChannelError("out_of_scope", "The repository is not reachable any more.", status=403) from None
        raise ChannelError("provider_unavailable", "Could not read the repository from GitHub.", status=503) from exc
    owner = ((data.get("owner") or {}).get("login") or "").lower()
    # The owner recorded for this repository when it was added: the App's
    # account, or for a personal token whoever owns the repository (a token
    # may work on another account's or an organization's repository).
    recorded = conn.settings.repo(repository_id)
    expected = (recorded.full_name.split("/", 1)[0] if recorded else conn.settings.owner.login).lower()
    if owner != expected:
        raise ChannelError("repository_moved", f"The repository now belongs to {owner or 'someone else'}, not "
                           f"{expected}. Update the channel file.", status=409)
    _NAMES[key] = (time.time() + NAME_SECONDS, data["full_name"])
    return data["full_name"]
