"""Building GitZoid's clients from the hub, and the few calls that touch the
hub itself: team or personal pages, list email and the daily model cap."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import github as github_mod
from .sample import SampleGitHub
from .security import Feeds
from .store import Store

log = logging.getLogger(__name__)
_APP_AUTH = {}


class CapReached(RuntimeError):
    pass


def sample_root(hub_dir: Path) -> Path:
    return Path(hub_dir) / "raw_data" / "sample"


def make_github(settings, store: Store | None, hub_dir: Path, token: str | None = None, transport=None, secrets: dict | None = None):
    if settings.source == "sample":
        return SampleGitHub(sample_root(hub_dir))
    token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
    credentials = secrets if secrets is not None else os.environ
    app = None
    names = ("GITHUB_APP_ID", "GITHUB_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY")
    if any(credentials.get(n) for n in names):
        if not all(credentials.get(n) for n in names):
            raise github_mod.GitHubError("App authentication needs GITHUB_APP_ID, GITHUB_INSTALLATION_ID and GITHUB_APP_PRIVATE_KEY")
        values = tuple(credentials[n].strip() for n in names)
        key = digest_of(values, id(transport))
        if key not in _APP_AUTH:
            _APP_AUTH[key] = github_mod.AppAuth(*values, transport=transport)
        app = _APP_AUTH[key]
    return github_mod.GitHub(token.strip(), store=store, dry_run=not settings.review.post_to_github,
                             transport=transport, app_auth=app)


def make_feeds(settings, hub_dir: Path) -> Feeds:
    return Feeds(sample_root(hub_dir) / "osv" if settings.source == "sample" else None)


def day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def hour(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H")


def take_model_call(store: Store, settings, n: int = 1) -> None:
    """Count model calls against limits.model_calls_per_day; raise when the
    cap is reached so the run stops cleanly and says why."""
    if not store.take("model_calls", day(), settings.limits.model_calls_per_day, n):
        raise CapReached(f"the daily model-call cap ({settings.limits.model_calls_per_day}) is reached; "
                         "raise limits.model_calls_per_day in workflows/settings.yaml or wait for tomorrow")


def write_page(hub, name: str, html: str) -> Path:
    path = Path(hub.run_dir) / f"{re.sub(r'[^a-z0-9-]+', '-', name.lower())}.html"
    path.write_text(html)
    return path


def digest_of(*data) -> str:
    """A fingerprint of the data behind a page (not its HTML, which carries
    timestamps), so Hubzoid skips storing a copy when nothing changed."""
    import hashlib
    import json
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def publish(hub, name: str, html: str, title: str, key: str, team_audience: str = "domain",
            digest: str | None = None) -> dict:
    """A team page when the run acts as GitZoid's automation account
    (hub.team_run), else a page only the person can open. A team page goes to
    the company domain; if domain sharing is not configured it falls back to
    everyone who can use the agent, and the result says so."""
    path = write_page(hub, name, html)
    if not hub.team_run:
        return {**hub.publish_artifact(path, title=title, key=key, audience="owner", digest=digest), "audience": "owner"}
    if team_audience == "domain":
        from hubzoid.artifacts import ArtifactError
        try:
            return {**hub.publish_artifact(path, title=title, key=key, audience="domain", digest=digest),
                    "audience": "domain"}
        except ArtifactError as exc:
            log.warning("domain sharing unavailable (%s); sharing %s with agent users", exc, key)
    return {**hub.publish_artifact(path, title=title, key=key, audience="hub", digest=digest), "audience": "hub"}


def email_list(hub, list_name: str | None, subject: str, html: str, art: dict | None) -> dict | None:
    """Email an admin-set list (workflows/settings.yaml email_lists) one of
    mail.py's emails, with the page linked. No list configured: nothing is
    sent and the result says so."""
    if not list_name:
        return None
    configured_lists = hub.setting("email_lists", {}) or {}
    if list_name in configured_lists and not configured_lists[list_name]:
        return None  # an intentionally empty list means email is off
    return hub.send_email(subject[:200], html, artifacts=[art] if art else (), email_list=list_name,
                          raise_on_failure=False, body_format="html")


def refresh_only(hub) -> bool:
    """Update now on a live page reruns its workflow to refresh the page. The
    list was emailed when the report was made, so it is not emailed again."""
    return hub.trigger == "update"


def delivered(result) -> bool:
    """Whether list email reached the list (or there was no list). Hubzoid
    sends one message per address and says "partial" when some addresses were
    refused (not an account here, or blocked); those reached are not sent the
    same alert again, and the refusals are in the delivery log."""
    if result is None:
        return True
    if not isinstance(result, dict):
        return False
    if result.get("status") == "partial":
        log.warning("list email reached only some addresses: %s", result.get("message"))
        return True
    return result.get("status") in ("accepted", "previewed")


def due_coverage(rows: list[dict], days: int = 3) -> list[dict]:
    """Checks that could not run and were not reported in the last `days`."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [c for c in rows if c.get("last_error") and (not c.get("alerted_at") or c["alerted_at"] < cutoff)]


def pr_repos(settings) -> set[str]:
    """Repos whose owner/repo#123 mentions link to GitHub in emails; none for
    the fictional sample."""
    return set(settings.repo_names()) if settings.source == "github" else set()


def link_of(art: dict | None) -> str:
    if not art:
        return ""
    return art.get("share_url") or art.get("latest_url") or art.get("url") or ""
