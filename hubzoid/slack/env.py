"""Validation for the two Slack tokens the adapter needs at startup."""
from __future__ import annotations

from typing import Mapping


class EnvError(RuntimeError):
    """Raised when SLACK_* env vars are missing or look wrong."""


_HOWTO_TAIL = (
    "Run `hubzoid slack manifest <hub>` to generate a manifest you can paste "
    "into https://api.slack.com/apps. After installing the app to your "
    "workspace, copy the Bot User OAuth Token (xoxb-...) and the App-Level "
    "Token (xapp-..., scope `connections:write`) into <hub>/.env."
)


def validate_env(env: Mapping[str, str]) -> None:
    """Refuse to start if Slack tokens are missing or pasted swapped.

    Raises `EnvError` with operator-facing guidance — never a bare KeyError.
    """
    bot = (env.get("SLACK_BOT_TOKEN") or "").strip()
    appt = (env.get("SLACK_APP_TOKEN") or "").strip()
    missing: list[str] = []
    if not bot:
        missing.append("SLACK_BOT_TOKEN")
    if not appt:
        missing.append("SLACK_APP_TOKEN")
    if missing:
        raise EnvError(
            f"Slack adapter cannot start: {', '.join(missing)} not set in .env.\n"
            f"{_HOWTO_TAIL}"
        )
    if not bot.startswith("xoxb-"):
        raise EnvError(
            "SLACK_BOT_TOKEN should start with `xoxb-` (Bot User OAuth Token). "
            f"Got a token starting with `{bot[:5]}...`. Did you swap it with "
            f"SLACK_APP_TOKEN?\n{_HOWTO_TAIL}"
        )
    if not appt.startswith("xapp-"):
        raise EnvError(
            "SLACK_APP_TOKEN should start with `xapp-` (App-Level Token, scope "
            f"`connections:write`). Got `{appt[:5]}...`. Did you paste the bot "
            f"token here by mistake?\n{_HOWTO_TAIL}"
        )


def should_start_slack(
    *,
    want_slack: bool,
    env: Mapping[str, str],
) -> tuple[bool, str | None]:
    """Decide whether `hubzoid run --slack` should spawn the Slack child.

    Returns `(start, warning)`:
      - `(False, None)`  when the operator didn't ask for Slack.
      - `(True,  None)`  when they did ask and tokens are valid.
      - `(False, msg)`   when they did ask but tokens are missing/wrong.
        The caller should print `msg` and continue without Slack — the
        whole point of this flag is to be best-effort, not crash the
        bridge + UI on a misconfigured `.env`.
    """
    if not want_slack:
        return False, None
    try:
        validate_env(env)
    except EnvError as exc:
        return False, str(exc)
    return True, None
