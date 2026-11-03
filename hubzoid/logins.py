"""Which Claude Code / Codex login a person has, what plan it is on, and which
one to prefer. Runtime-neutral: detection lives in factory_claude / factory_codex
and returns the `Login` record defined here. Never holds a token."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Login:
    runtime: str                 # "claude-local" | "codex-local"
    signed_in: bool
    method: str | None = None    # "subscription" | "api_key"
    plan: str | None = None      # as the CLI reports it: "team", "max", "pro", "plus", ...
    tier: int = 0                # 2 top, 1 standard, 0 unknown or API key
    version: str | None = None
    problem: str | None = None   # "not_file_backed" | None
    fix: str | None = None       # one command that fixes `problem`
    api_key_in_env: bool = False  # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN is set in the environment


# Plan names as the CLIs report them: Claude Code 2.1.301 `subscriptionType`
# (max, pro, team, enterprise, free) and Codex 0.163 `chatgpt_plan_type` (KnownPlan).
# free, hc and anything unknown are tier 0.
TOP = {
    "claude-local": {"max", "team", "enterprise"},
    "codex-local": {"pro", "promax", "team", "business", "self_serve_business_prolite",
                    "self_serve_business_usage_based", "enterprise", "ent26",
                    "enterprise_cbp_automation", "enterprise_cbp_usage_based", "edu_pro"},
}
STANDARD = {
    "claude-local": {"pro"},
    "codex-local": {"plus", "prolite", "go", "edu", "education", "edu_plus"},
}

_NAMES = {"claude-local": "Claude Code", "codex-local": "Codex"}

# Plan names people know; any other is its words, capitalised.
_PLAN_NAMES = {
    "prolite": "Pro Lite", "promax": "Pro Max",
    "self_serve_business_prolite": "Self-serve Business Pro Lite",
    "self_serve_business_usage_based": "Self-serve Business Usage-based",
    "ent26": "Enterprise", "enterprise_cbp_automation": "Enterprise", "enterprise_cbp_usage_based": "Enterprise",
}

# What Diagnostics and init say about a login that is there but can't be used.
_PROBLEMS = {"not_file_backed": "{name} is signed in, but not in a file the runtime can use."}


def tier(runtime: str, method: str | None, plan: str | None) -> int:
    """2 top, 1 standard, 0 an API key or a plan we don't know."""
    if method != "subscription" or not plan:
        return 0
    plan = plan.strip().lower()
    if plan in TOP.get(runtime, ()):
        return 2
    return 1 if plan in STANDARD.get(runtime, ()) else 0


def detect() -> list[Login]:
    """Each installed CLI's login; a CLI that isn't installed is left out."""
    from . import factory_claude, factory_codex

    found = (factory_claude.claude_login(), factory_codex.codex_login())
    return [login for login in found if login is not None]


def choose(logins: list[Login]) -> Login | None:
    """Subscriptions before API keys, then the higher tier, then Claude."""
    usable = [lg for lg in logins if lg.signed_in and not lg.problem]
    if not usable:
        return None
    return max(usable, key=lambda lg: (lg.method == "subscription", lg.tier, lg.runtime == "claude-local"))


def plan_name(plan: str) -> str:
    key = plan.strip().lower()
    return _PLAN_NAMES.get(key) or " ".join(w.capitalize() for w in re.split(r"[_\s-]+", key) if w)


def describe(login: Login) -> str:
    name = _NAMES.get(login.runtime, login.runtime)
    if login.method == "api_key":
        return f"{name}, API key"
    return f"{name}, {plan_name(login.plan)} plan" if login.plan else name


def problem(login: Login) -> str | None:
    """One plain sentence for the login's ``problem``, or None."""
    text = _PROBLEMS.get(login.problem or "")
    return text.format(name=_NAMES.get(login.runtime, login.runtime)) if text else None
