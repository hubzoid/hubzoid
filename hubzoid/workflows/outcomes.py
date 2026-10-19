# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""What happens after a run ends: which report series are still live, the
personal schedule's bookkeeping and notices, and the "your report is ready"
email for a Run for me run that outlived the chat's wait.

Called from the workflow wrapper after the author's function returns or
raises. Best effort: a failure here is logged and never changes the run's
own outcome. Everything is idempotent per run id, so a replayed run doesn't
count or notify twice.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("hubzoid.workflows")

#: Fixed wording for why a run stopped. Raw errors can hold secrets or data,
#: so chat and email only ever say one of these; the Console has the detail.
CAUSES = {
    "timeout": "it timed out",
    "access": "access changed",
    "inputs": "inputs changed",
    "removed": "the workflow was removed",
    "error": "it stopped with an error",
    "dispatch": "it could not be queued",
}


def cause_of(error: BaseException | None) -> str:
    """The fixed reason code for a failed run."""
    if error is None:
        return "error"
    from .identity import IdentityError
    from .inputs import InputError

    if isinstance(error, IdentityError):
        return "access"
    if isinstance(error, InputError):
        return "inputs"
    name = type(error).__name__.lower()
    if "timeout" in name or "deadline" in name:
        return "timeout"
    return "error"


def finished(hub_dir, hub: str, workflow: str, run_id: str, env: dict, *, ok: bool,
             error: BaseException | None = None, published: list | None = None,
             tool_wait: float = 60.0, result=None) -> None:
    hub_dir = Path(hub_dir)
    trigger = env.get("trigger")
    try:
        from ..artifacts import series

        series.finish_run(hub_dir, hub, workflow, run_id, trigger=trigger,
                          scope=env.get("scope"), schedule_id=env.get("schedule_id"), ok=ok)
    except Exception:  # noqa: BLE001
        log.exception("workflows: could not update report links after run %s", run_id)
    cause = None if ok else cause_of(error)
    try:
        if trigger == "personal_schedule" and env.get("schedule_id"):
            from . import personal

            personal.after_run(hub_dir, hub, env, run_id, ok=ok, cause=cause,
                               published=list(published or []))
        elif trigger == "tool" and ok and env.get("acts_as"):
            from .personal import email_on

            started = env.get("started_at") or 0
            if started and time.time() - started > tool_wait and email_on(hub_dir):
                _ready_email(hub_dir, hub, workflow, run_id, env, list(published or []))
    except Exception:  # noqa: BLE001
        log.exception("workflows: could not finish bookkeeping for run %s", run_id)
    if env.get("chat"):  # started from a chat: the outcome goes back there
        from .chat_link import post_result

        post_result(hub_dir, env, workflow, run_id, ok=ok, result=result, cause=cause,
                    published=list(published or []), tool_wait=tool_wait)


def links_text(published: list) -> str:
    seen, lines = set(), []
    for item in published:
        url = item.get("url")
        if url and url not in seen:
            seen.add(url)
            lines.append(f"{item.get('title') or 'Report'}: {url}")
    return "\n".join(lines)


def _ready_email(hub_dir: Path, hub: str, workflow: str, run_id: str, env: dict,
                 published: list) -> None:
    from .. import email_delivery
    from .identity import RunIdentity
    from .runtime import title_for

    who = env["acts_as"]
    title = title_for(workflow)
    links = links_text(published) or "It finished. Ask me for the report in chat."
    email_delivery.send_to_owner(
        hub_dir, hub=hub, identity=RunIdentity(who, env.get("acts_as_account"), "caller", who),
        subject=f"Your {title} is ready",
        body=f"The report you asked for in chat is ready.\n\n{links}\n",
        workflow=workflow, run_id=run_id, idem_key=f"notice:{run_id}:ready")
