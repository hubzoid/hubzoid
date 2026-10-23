# Hubzoid published artifacts. Apache-2.0 licensed like the rest of the repository.
"""Company-domain sharing: which email domains may read a report through its
company link, and whether this deployment can offer it at all.

`HUBZOID_SHARE_DOMAINS=company.org` (comma separated) names the domains. They
are a separate boundary from who may sign in: company links stay off unless
company sign-in (Google, Microsoft or a standard OIDC provider) is set up with
signup on and each share domain listed explicitly in `OAUTH_ALLOWED_DOMAINS`,
so a reader arriving through a link gets an active account.
"""
from __future__ import annotations

import os
from typing import Mapping

ENV = "HUBZOID_SHARE_DOMAINS"


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def share_domains(env: Mapping[str, str] | None = None) -> list[str]:
    raw = _env(env).get(ENV) or ""
    return sorted({d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()})


def status(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """(on, why not). The reason is one sentence an admin can act on."""
    from ..auth import oidc

    env = _env(env)
    domains = share_domains(env)
    if not domains:
        return False, f"Company links are off. Set {ENV} to your company's email domain."
    label = ", ".join(domains)
    if not oidc.providers(env):
        return False, (f"Company links are off. They need company sign-in (Google or Microsoft) "
                       f"for {label}. See the setup guide.")
    if not oidc.signup_enabled(env):
        return False, (f"Company links are off. Turn on ENABLE_OAUTH_SIGNUP so people at "
                       f"{label} get an account when they first open a link.")
    allowed = oidc.allowed_domains(env)
    missing = [d for d in domains if allowed is None or d not in allowed]
    if missing:
        return False, (f"Company links are off. {ENV} lists {', '.join(missing)}, but company "
                       "sign-in doesn't allow it. Add it to OAUTH_ALLOWED_DOMAINS, or remove it.")
    return True, ""


def status_for(hub_dir, hub: str, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """`status` for one agent. An agent that an older "everyone signed in"
    grant opens can't have company links: with sign-up on, every reader would
    get the agent too."""
    on, why = status(env)
    if not on:
        return on, why
    from ..access import store_for

    if store_for(hub_dir).everyone_can_use(hub):
        return False, ("Company links are off for this agent: everyone signed in can use it. "
                       "Give people access by name in Settings first.")
    return True, ""


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return status(env)[0]


def label(env: Mapping[str, str] | None = None) -> str:
    """The domains as people read them: "sadhguru.org"."""
    return ", ".join(share_domains(env)) or "your company domain"


def email_allowed(email: str | None, env: Mapping[str, str] | None = None) -> bool:
    """Whether `email` is at one of the share domains (and sharing is on)."""
    email = (email or "").strip().lower()
    if "@" not in email or not enabled(env):
        return False
    return email.rsplit("@", 1)[1] in share_domains(env)
