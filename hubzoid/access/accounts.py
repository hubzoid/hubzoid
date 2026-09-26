# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""The chat-app account directory: create, update and delete login accounts.

Open WebUI owns credentials. Hubzoid only calls its supported admin API as the
deployment's service account (`HUBZOID_GATEWAY_ADMIN_EMAIL`/`_PASSWORD`) on the
internal URL, never its database and never through the public edge. A frontend
other than Open WebUI would replace this module (and `access.session`).

Open WebUI endpoints used (0.11.4):
  * `POST /api/v1/auths/signin`          service-account token
  * `POST /api/v1/auths/add`             create (always with role "user")
  * `GET  /api/v1/users/?query=`         find the account using an email
  * `GET  /api/v1/users/{id}`            read one account before changing it
  * `POST /api/v1/users/{id}/update`     password, role or name
  * `DELETE /api/v1/users/{id}`          delete

Passwords travel only in request bodies to Open WebUI. They are never logged,
stored, returned or put into an error message. The token Open WebUI returns
for a new account is discarded.

Google sign-in only. Open WebUI 0.11.4 attaches a Google sign-in to an
existing account with the same email only when `OAUTH_MERGE_ACCOUNTS_BY_EMAIL`
is true (`utils/oauth.py`, merge by email). It does not check the provider's
`email_verified` claim, so only Google, whose account emails are verified, is
offered; generic OIDC and Microsoft are not. Such an account is created through
the same supported `auths/add` call with a random password generated here and
never returned, shown, stored or logged (`unusable_password`), so nobody can
sign in with a password and Google attaches on first sign-in.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger("hubzoid.access")

_TIMEOUT = httpx.Timeout(10.0)


class AccountError(Exception):
    """A directory call failed. `code` is stable for callers; `message` is safe
    to show (it never contains a password). `certain` is False when the request
    may have taken effect (no response was received)."""

    def __init__(self, status: int, code: str, message: str, *, certain: bool = True):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.certain = certain


class AccountsUnavailable(AccountError):
    def __init__(self, message: str):
        super().__init__(503, "accounts_unavailable", message)


class AccountDirectory(Protocol):
    def create(self, *, email: str, name: str, password: str, role: str = "user") -> dict: ...
    def update(self, account_id: str, *, password: str | None = None,
               role: str | None = None, name: str | None = None) -> None: ...
    def delete(self, account_id: str) -> None: ...
    def get(self, account_id: str) -> dict | None: ...
    def find(self, email: str) -> dict | None: ...


def unusable_password() -> str:
    """A password nobody is told, for an account that signs in with Google only.

    About 300 random bits, within bcrypt's 72 bytes, and it always meets Open
    WebUI's optional strength rule (lower, upper, digit, symbol). The caller
    passes it straight to the chat app and drops it: it is never returned,
    shown, stored or logged."""
    return secrets.token_urlsafe(40)[:50] + "-Aa1"


def sign_in_options(hub_dir: Path) -> dict:
    """How a new account can sign in: {"password": True, "google": bool} plus
    "google_domains" when the chat app accepts Google sign-in only for some
    domains. Read without a network call.

    "google" is true only when the chat app attaches a Google sign-in to an
    existing account by email: Google configured, `OAUTH_MERGE_ACCOUNTS_BY_EMAIL`
    true and OAuth settings taken from the environment. A gateway records these
    facts (never the values) in its deployment manifest, because bridges started
    separately (`gateway --no-bridges`) may not share its environment; a
    standalone hub reads its own. Nothing here changes the chat app's policy."""
    from .. import deployment

    try:
        manifest = deployment.read(Path(hub_dir))
    except Exception:  # noqa: BLE001 — unreadable: offer nothing that depends on it
        log.warning("accounts: deployment manifest unreadable; Google sign-in not offered")
        return {"password": True, "google": False}
    flags = (manifest.get("sign_in") or {}) if manifest else deployment.sign_in_flags(_env(hub_dir))
    google = (bool(flags.get("google")) and bool(flags.get("merge_by_email"))
              and not flags.get("oauth_settings_in_app"))
    out: dict = {"password": True, "google": google}
    domains = flags.get("allowed_domains")
    if google and isinstance(domains, list):
        out["google_domains"] = [str(d) for d in domains]
    return out


def _env(hub_dir: Path) -> dict:
    from dotenv import dotenv_values

    return {**dotenv_values(Path(hub_dir) / ".env"), **os.environ}


def service_account_email(hub_dir: Path) -> str:
    """The deployment's Open WebUI service account (never offered for changes)."""
    return (_env(hub_dir).get("HUBZOID_GATEWAY_ADMIN_EMAIL") or "").strip().lower()


def internal_url(hub_dir: Path) -> str:
    """The server-to-server Open WebUI URL, or '' when only a public URL is known.

    The manifest's URL (gateway) or `OWUI_INTERNAL_URL` (`hubzoid run`) are
    internal. `WEBUI_URL` and `HUBZOID_PUBLIC_URL` are public: account writes
    through them would pass the edge, so they are refused here."""
    from .. import deployment

    try:
        configured = (deployment.read(Path(hub_dir)).get("owui_url") or "").rstrip("/")
    except Exception:  # noqa: BLE001 — an unreadable manifest is not a URL
        log.warning("accounts: deployment manifest unreadable; account writes unavailable")
        return ""
    explicit = (os.environ.get("OWUI_INTERNAL_URL") or "").rstrip("/")
    if configured and explicit and configured != explicit:
        return ""
    base = configured or explicit
    public = {
        (os.environ.get(k) or "").rstrip("/")
        for k in ("WEBUI_URL", "HUBZOID_PUBLIC_URL")
    } - {""}
    if not base or base in public:
        return ""
    return base


def configured(hub_dir: Path) -> bool:
    """Whether account management can run: an internal URL and service account
    credentials are present. No network call."""
    env = _env(hub_dir)
    return bool(
        internal_url(hub_dir)
        and env.get("HUBZOID_GATEWAY_ADMIN_EMAIL")
        and env.get("HUBZOID_GATEWAY_ADMIN_PASSWORD")
    )


def _detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    detail = data.get("detail") if isinstance(data, dict) else None
    return detail if isinstance(detail, str) else ""


def _safe(message: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:300]


class OwuiAccounts:
    """Open WebUI's admin account API, used as the service account."""

    def __init__(self, base_url: str, email: str, password: str,
                 *, transport: httpx.BaseTransport | None = None):
        if not base_url:
            raise AccountsUnavailable(
                "Account management needs the chat app's internal URL. Only a public "
                "URL is configured, so accounts cannot be changed from here."
            )
        if not email or not password:
            raise AccountsUnavailable(
                "Set HUBZOID_GATEWAY_ADMIN_EMAIL and HUBZOID_GATEWAY_ADMIN_PASSWORD to "
                "manage chat accounts from the Console."
            )
        self._base = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._transport = transport

    def _client(self) -> httpx.Client:
        from ..gateway_provision import ProvisionError, service_token

        client = httpx.Client(base_url=self._base, timeout=_TIMEOUT, transport=self._transport)
        try:
            token = service_token(client, self._email, self._password)
        except httpx.HTTPError as exc:
            client.close()
            raise AccountsUnavailable(
                "The chat app could not be reached. Try again shortly."
            ) from exc
        except ProvisionError as exc:
            client.close()
            log.warning("accounts: service account sign-in failed (HTTP %s)", exc.status)
            if exc.status == 429:
                raise AccountsUnavailable(
                    "The chat app is refusing sign-ins for the Console's service account "
                    "for a few minutes (too many recent sign-ins). Try again shortly."
                ) from None
            raise AccountsUnavailable(
                "The Console's service account could not sign in to the chat app. "
                "Check HUBZOID_GATEWAY_ADMIN_EMAIL and HUBZOID_GATEWAY_ADMIN_PASSWORD."
            ) from None
        client.headers["Authorization"] = f"Bearer {token}"
        return client

    def _send(self, method: str, path: str, *, json: dict | None = None,
              params: dict | None = None, secret: str | None = None, what: str,
              client: httpx.Client | None = None) -> httpx.Response:
        own = client is None
        client = client or self._client()
        try:
            r = client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise AccountError(
                503, "uncertain",
                f"The chat app did not answer while trying to {what}. It may or may "
                "not have happened. Refresh accounts before trying again.",
                certain=False,
            ) from exc
        finally:
            if own:
                client.close()
        if r.status_code >= 500:
            log.warning("accounts: %s failed (HTTP %s)", what, r.status_code)
            raise AccountError(
                502, "chat_app_error",
                f"The chat app failed to {what}. Try again shortly.",
                certain=False,
            )
        if r.status_code in (401, 403):
            raise AccountError(
                409, "refused",
                _safe(_detail(r), secret) or f"The chat app refused to {what}.",
            )
        return r

    def create(self, *, email: str, name: str, password: str, role: str = "user") -> dict:
        if role != "user":
            raise ValueError("accounts are created with the user role only")
        r = self._send(
            "POST", "/api/v1/auths/add",
            json={"name": name, "email": email, "password": password, "role": "user"},
            secret=password, what="create the account",
        )
        if r.status_code == 400:
            detail = _detail(r)
            if "already registered" in detail.lower() or "taken" in detail.lower():
                raise AccountError(409, "account_exists",
                                   "An account with this email already exists.")
            raise AccountError(
                422, "rejected",
                _safe(detail, password) or "The chat app rejected this account.",
            )
        if r.status_code != 200:
            raise AccountError(502, "chat_app_error",
                               f"The chat app returned HTTP {r.status_code}.")
        data = r.json()
        # The response carries a session token for the new account: never kept.
        return {k: data.get(k) for k in ("id", "email", "name", "role")}

    def get(self, account_id: str) -> dict | None:
        r = self._send("GET", f"/api/v1/users/{account_id}", what="read the account")
        if r.status_code in (400, 404):
            return None
        if r.status_code != 200:
            raise AccountError(502, "chat_app_error",
                               f"The chat app returned HTTP {r.status_code}.")
        data = r.json()
        return {k: data.get(k) for k in ("id", "email", "name", "role")}

    def find(self, email: str) -> dict | None:
        """The account using `email` (case-insensitive), or None. The chat app
        searches names and emails by substring; the exact match is picked here."""
        email = (email or "").strip().lower()
        if not email:
            return None
        client = self._client()
        try:
            for page in range(1, 21):  # 30 per page; an exact email matches few
                r = self._send("GET", "/api/v1/users/", params={"query": email, "page": page},
                               what="look up the account", client=client)
                if r.status_code != 200:
                    raise AccountError(502, "chat_app_error",
                                       f"The chat app returned HTTP {r.status_code}.")
                try:
                    data = r.json()
                    batch = data["users"]
                    total = int(data["total"])
                except (ValueError, KeyError, TypeError):
                    raise AccountError(502, "chat_app_error",
                                       "The chat app returned an unexpected account list.")
                for user in batch:
                    if isinstance(user, dict) and str(user.get("email") or "").strip().lower() == email:
                        return {k: user.get(k) for k in ("id", "email", "name", "role")}
                if not batch or page * 30 >= total:
                    return None
            return None
        finally:
            client.close()

    def update(self, account_id: str, *, password: str | None = None,
               role: str | None = None, name: str | None = None) -> None:
        body = {k: v for k, v in (("password", password), ("role", role), ("name", name))
                if v is not None}
        if not body:
            return
        r = self._send("POST", f"/api/v1/users/{account_id}/update", json=body,
                       secret=password, what="update the account")
        if r.status_code == 400:
            raise AccountError(422, "rejected",
                               _safe(_detail(r), password) or "The chat app rejected the change.")
        if r.status_code != 200:
            raise AccountError(502, "chat_app_error",
                               f"The chat app returned HTTP {r.status_code}.")

    def delete(self, account_id: str) -> None:
        r = self._send("DELETE", f"/api/v1/users/{account_id}", what="delete the account")
        if r.status_code == 400:
            raise AccountError(409, "not_found", "The chat account no longer exists.")
        if r.status_code != 200:
            raise AccountError(502, "chat_app_error",
                               f"The chat app returned HTTP {r.status_code}.")


def for_deployment(hub_dir: Path, *, transport: httpx.BaseTransport | None = None
                   ) -> AccountDirectory:
    """The deployment's account directory: internal URL and service account only.
    Raises AccountsUnavailable when either is missing."""
    env = _env(hub_dir)
    return OwuiAccounts(
        internal_url(hub_dir),
        env.get("HUBZOID_GATEWAY_ADMIN_EMAIL") or "",
        env.get("HUBZOID_GATEWAY_ADMIN_PASSWORD") or "",
        transport=transport,
    )
