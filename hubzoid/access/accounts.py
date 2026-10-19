# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""The chat-app account directory: create, update and delete login accounts.

``HubzoidAccounts`` implements ``AccountDirectory`` over Hubzoid's own
accounts (``hubzoid.auth.users``) in the shared operational store. A new
account needs no password: the Console hands out a one-time sign-in link
(``issue_link``) instead. Passwords are never logged, stored in plain text,
returned or put into an error message.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Protocol

log = logging.getLogger("hubzoid.access")

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
    # Optional: emails of every account with the chat-app role "admin". A
    # directory without it skips the chat-app side of the last-admin check.
    # def admins(self) -> list[str]: ...


def unusable_password() -> str:
    """A password nobody is told, for an account that signs in with Google only.

    About 300 random bits, within bcrypt's 72 bytes, and it always meets Open
    WebUI's optional strength rule (lower, upper, digit, symbol). The caller
    passes it straight to the chat app and drops it: it is never returned,
    shown, stored or logged."""
    return secrets.token_urlsafe(40)[:50] + "-Aa1"


def sign_in_options(hub_dir: Path) -> dict:
    """How a new account can sign in, read without a network call:
    {"password": bool, "google": bool, "links": True} plus "google_domains" when
    Google sign-in is limited to some domains. A new account (and a password
    reset) gets a one-time sign-in link. "google" is true when Google is
    configured and ``OAUTH_MERGE_ACCOUNTS_BY_EMAIL`` lets a Google sign-in
    attach to the account by its verified email."""
    from ..auth import oidc
    from ..auth.routes import password_login_enabled

    env = _env(hub_dir)
    google = oidc.provider("google", env) is not None and oidc.merge_by_email(env)
    out: dict = {"password": password_login_enabled(), "google": google, "links": True}
    domains = oidc.allowed_domains(env)
    if google and domains is not None:
        out["google_domains"] = domains
    return out


def _env(hub_dir: Path) -> dict:
    from dotenv import dotenv_values

    return {**dotenv_values(Path(hub_dir) / ".env"), **os.environ}


_STORE_DOWN = "Accounts are unavailable right now. Try again shortly."


class HubzoidAccounts:
    """Hubzoid's own accounts as an ``AccountDirectory`` (default mode).

    Returned dicts have the Open WebUI adapter's shape, ``{"id", "email",
    "name", "role"}``, with ``role`` "pending" while an account awaits
    approval, so ``access.service`` runs its approve, role and delete flows
    unchanged. They add ``"sign_in"``: "password", or "google" for an account
    that signs in with an external provider only.

    Writes follow the account rules in ``hubzoid.auth.users``: a password or
    role change and a deletion end the person's sessions. New accounts are
    created without a password; ``issue_link`` gives the one-time link to set
    one. ``links`` tells ``access.service`` to hand out links, not passwords."""

    links = True

    def __init__(self, hub_dir: Path):
        self.hub_dir = Path(hub_dir)

    def _store(self):
        from ..auth import users

        try:
            return users.store(self.hub_dir)
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("accounts: account store unavailable")
            raise AccountsUnavailable(_STORE_DOWN) from exc

    def _run(self, fn, *args, **kwargs):
        from sqlalchemy.exc import SQLAlchemyError

        try:
            return fn(*args, **kwargs)
        except SQLAlchemyError as exc:
            log.warning("accounts: account store error")
            raise AccountError(503, "accounts_unavailable", _STORE_DOWN, certain=False) from exc

    @staticmethod
    def _view(user: dict | None) -> dict | None:
        if user is None or user.get("status") == "deleted":
            return None  # a deleted account's tombstone is not an account here
        from ..auth.users import sign_in_of

        return {"id": user["id"], "email": user["email"], "name": user.get("name") or "",
                "role": "pending" if user["status"] == "pending" else user["role"],
                "sign_in": sign_in_of(user)}

    def create(self, *, email: str, name: str, password: str | None = None,
               role: str = "user", password_enabled: bool = True) -> dict:
        """A new active account with the user role. ``password`` is optional:
        without one the person sets it through a one-time link."""
        from ..auth import passwords, users

        if role != "user":
            raise ValueError("accounts are created with the user role only")
        if users.is_local_address(email):
            raise AccountError(422, "rejected", "A localhost address can't sign in. Use a real email.")
        st = self._store()
        try:
            user = self._run(st.create, email=email, name=name, role="user", status="active",
                             password=password or None, password_enabled=password_enabled,
                             source="admin")
        except users.AccountExists:
            raise AccountError(409, "account_exists", "An account with this email already exists.")
        except users.InvalidAccount as exc:
            raise AccountError(422, "rejected", exc.message)
        except passwords.PasswordRejected as exc:
            raise AccountError(422, "invalid_password", exc.message)
        return self._view(user)  # type: ignore[return-value]

    def get(self, account_id: str) -> dict | None:
        return self._view(self._run(self._store().get, account_id))

    def find(self, email: str) -> dict | None:
        return self._view(self._run(self._store().find_by_email, email))

    def admins(self) -> list[str]:
        """Emails of active administrators who can sign in."""
        from ..auth import users

        return [u["email"] for u in self._run(users.admins, self.hub_dir)]

    def update(self, account_id: str, *, password: str | None = None,
               role: str | None = None, name: str | None = None) -> None:
        """``role`` "user" or "admin" also approves a pending account; "pending"
        puts an account back to awaiting approval."""
        from ..auth import passwords, users

        st = self._store()
        if self._run(st.get, account_id) is None:
            raise AccountError(409, "not_found", "The account no longer exists.")
        try:
            if name is not None:
                self._run(st.set_name, account_id, name)
            if role is not None:
                if role == "pending":
                    self._run(st.set_status, account_id, "pending")
                elif role in users.ROLES:
                    self._run(st.set_role, account_id, role)
                    self._run(st.set_status, account_id, "active")
                else:
                    raise AccountError(422, "rejected", "Choose User or Administrator.")
            if password is not None:
                self._run(st.set_password, account_id, password)
        except users.InvalidAccount as exc:
            raise AccountError(422, "rejected", exc.message)
        except passwords.PasswordRejected as exc:
            raise AccountError(422, "invalid_password", exc.message)
        except KeyError:
            raise AccountError(409, "not_found", "The account no longer exists.")

    def delete(self, account_id: str) -> None:
        """Delete the account (a tombstone that keeps their conversations), its
        sessions, external sign-ins, links, MFA, keys and personal connection tokens."""
        from ..auth import users

        self._store()
        if not self._run(users.delete, self.hub_dir, account_id):
            raise AccountError(409, "not_found", "The account no longer exists.")

    def discard(self, account_id: str) -> None:
        """Remove an account created moments ago that was never used (no tombstone)."""
        from ..auth import users

        if not self._run(lambda: users.store(self.hub_dir).purge(account_id)):
            raise AccountError(409, "not_found", "The account no longer exists.")

    # ---- default mode only ---------------------------------------------------------

    def issue_link(self, account_id: str, *, purpose: str = "set_password",
                   created_by: str | None = None) -> dict:
        """A one-time link for the person to set their password:
        ``{"link", "expires_at"}``. ``link`` is absolute when the public URL is
        configured (``HUBZOID_PUBLIC_URL``), else a path on
        this site. Earlier unused links for the account stop working."""
        from .. import appmode
        from ..auth import links

        try:
            token, expires = self._run(links.create, self.hub_dir, account_id, purpose=purpose,
                                       created_by=created_by)
        except KeyError:
            raise AccountError(409, "not_found", "The account no longer exists.")
        except ValueError:
            raise AccountError(409, "local_owner", "This is the local owner of a server without "
                                                   "sign-in. It has no password to set.")
        return {"link": links.url(token, appmode.public_url()), "expires_at": expires}

    def reset_with_link(self, account_id: str, *, created_by: str | None = None) -> dict:
        """Reset a password: the current one stops working and every session
        ends now; the returned link sets a new one."""
        from ..auth import users

        st = self._store()
        account = self._run(st.get, account_id)
        if account is None:
            raise AccountError(409, "not_found", "The account no longer exists.")
        if users.is_local_address(account["email"]):
            raise AccountError(409, "local_owner", "This is the local owner of a server without "
                                                   "sign-in. It has no password to set.")
        try:
            self._run(st.set_password, account_id, None)
        except KeyError:
            raise AccountError(409, "not_found", "The account no longer exists.")
        return self.issue_link(account_id, purpose="reset_password", created_by=created_by)

    def directory(self) -> list[dict]:
        """Every account, shaped like ``access.owui.directory`` rows, for an
        account refresh (``GrantStore.reconcile_accounts``)."""
        rows = []
        for user in self._run(self._store().list):
            rows.append(dict(subject=user["email"], email=user["email"], display=user["name"],
                             owui_id=user["id"],
                             role="pending" if user["status"] == "pending" else user["role"]))
        return rows


def for_deployment(hub_dir: Path) -> AccountDirectory:
    """The deployment's account directory."""
    return HubzoidAccounts(hub_dir)
