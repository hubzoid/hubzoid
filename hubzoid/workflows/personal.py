# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""Personal schedules: a person keeps a report fresh from chat.

A schedule saves one workflow, its inputs, a cadence and a timezone for its
owner. It always runs as that owner, with their grants, checked again before
every run. It never runs as anyone else, and it can't be moved to someone else.

Saving, changing and stopping take two calls: a proposal, then a confirm with
the code it returned, within 10 minutes and only by the same person. That stops
a model saving a schedule in one call by mistake. It doesn't prove a human
agreed, so every new schedule also emails its owner.

The hub's dispatcher fires due schedules every minute: it starts the run with
an id made of the schedule and its slot, then advances `next_fire` with a
compare-and-swap, so a slot runs at most once even across a crash. After
downtime a schedule runs at most once late. A schedule that can't run pauses
with a named reason instead of failing over and over: the owner's access
changed, its inputs no longer match the workflow, the workflow was removed,
or three runs failed in a row.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

log = logging.getLogger("hubzoid.workflows")

SCHEDULE_PERMISSION = "schedule_reports"
PROPOSAL_SECONDS = 600
LIVE_STATES = ("testing", "active", "paused")
DEFAULTS = {"min_interval": 3600.0, "per_person": 10, "pause_after_failures": 3}
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ScheduleError(ValueError):
    """A refused schedule action. The message is safe to show in chat."""


@dataclass(frozen=True)
class Schedule:
    id: str
    hub: str
    workflow: str
    owner: str
    owner_account: str | None
    name: str
    inputs: str
    cadence: str
    timezone: str
    state: str
    reason: str | None
    paused_by: str | None
    next_fire: float | None
    last_fire: float | None
    last_run: str | None
    last_status: str | None
    last_at: float | None
    last_cause: str | None
    failures: int
    created: float
    updated: float

    @property
    def values(self) -> dict:
        try:
            return json.loads(self.inputs) or {}
        except ValueError:
            return {}


_COLS = ", ".join(Schedule.__dataclass_fields__)


def _engine(hub_dir):
    from .. import db
    from ..migrations import upgrade

    eng = db.operational_engine(Path(hub_dir))
    upgrade(eng, "operational")
    return eng


def settings(hub_dir) -> dict:
    """`personal_schedules:` in workflows/settings.yaml, over the defaults."""
    from .deadlines import seconds
    from .events import settings as wf_settings

    try:
        raw = wf_settings(Path(hub_dir)).get("personal_schedules") or {}
    except Exception:  # noqa: BLE001 — a broken settings file keeps the defaults
        raw = {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        if raw.get("min_interval") is not None:
            out["min_interval"] = float(seconds(raw["min_interval"]))
        for key in ("per_person", "pause_after_failures"):
            if raw.get(key) is not None:
                out[key] = max(1, int(raw[key]))
    return out


def email_on(hub_dir) -> bool:
    """Email people about their own schedules and long runs: off unless the hub
    sets `notify_by_email: true` in workflows/settings.yaml and email delivery
    is set up. Chat and the report pages show the same state either way."""
    from .. import email_delivery
    from .events import settings as wf_settings

    try:
        if wf_settings(Path(hub_dir)).get("notify_by_email") is not True:
            return False
        return email_delivery.config_problem(email_delivery.config()) is None
    except Exception:  # noqa: BLE001 — a broken settings file or setup sends nothing
        return False


def get(hub_dir, schedule_id: str | None) -> Schedule | None:
    if not schedule_id:
        return None
    with _engine(hub_dir).connect() as c:
        r = c.execute(text(f"SELECT {_COLS} FROM hz_personal_schedules WHERE id=:i"),
                      {"i": schedule_id}).fetchone()
    return Schedule(**dict(r._mapping)) if r else None


def for_owner(hub_dir, hub: str, owner: str, *, include_stopped: bool = False) -> list[Schedule]:
    q = f"SELECT {_COLS} FROM hz_personal_schedules WHERE hub=:h AND owner=:o"
    if not include_stopped:
        q += " AND state<>'stopped'"
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text(q + " ORDER BY created"), {"h": hub, "o": owner}).fetchall()
    return [Schedule(**dict(r._mapping)) for r in rows]


def for_hub(hub_dir, hub: str) -> list[Schedule]:
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text(f"SELECT {_COLS} FROM hz_personal_schedules WHERE hub=:h "
                              "AND state<>'stopped' ORDER BY owner, created"), {"h": hub}).fetchall()
    return [Schedule(**dict(r._mapping)) for r in rows]


def _update(hub_dir, schedule_id: str, **values) -> None:
    values["updated"] = time.time()
    sets = ", ".join(f"{k}=:{k}" for k in values)
    with _engine(hub_dir).begin() as c:
        c.execute(text(f"UPDATE hz_personal_schedules SET {sets} WHERE id=:id"),
                  dict(values, id=schedule_id))


# ---- words ------------------------------------------------------------------------


def _when(ts: float | None, tz: str) -> str:
    """"Mon 12 Oct, 09:00" in the schedule's zone."""
    if not ts:
        return "not scheduled"
    from zoneinfo import ZoneInfo

    dt = datetime.fromtimestamp(ts, tz=ZoneInfo(tz))
    return f"{dt:%a} {dt.day} {dt:%b}, {dt:%H:%M}"


def cadence_words(sched_or_cadence, tz: str | None = None) -> str:
    from .schedule_grammar import describe

    cadence = getattr(sched_or_cadence, "cadence", sched_or_cadence)
    return describe(cadence)


def state_words(s: Schedule) -> str:
    if s.state == "testing":
        return "First run in progress"
    if s.state == "active":
        return "Active"
    if s.state == "stopped":
        return "Stopped"
    return {
        "owner": "Paused by owner", "admin": "Paused by admin",
        "failures": "Paused after failures", "access": "Paused: access changed",
        "inputs": "Paused: inputs changed", "first_run": "Paused: first run failed",
        "removed": "Paused: the workflow was removed",
    }.get(s.reason or "", "Paused")


def last_words(s: Schedule) -> str:
    from .outcomes import CAUSES

    if s.last_status == "updated":
        return f"Updated {_when(s.last_at, s.timezone)}"
    if s.last_status == "no_change":
        return f"No change {_when(s.last_at, s.timezone)}"
    if s.last_status == "failed":
        return f"Failed {_when(s.last_at, s.timezone)}: {CAUSES.get(s.last_cause or '', CAUSES['error'])}"
    if s.last_status == "running":
        return "Running now"
    return "Not run yet"


# ---- checks -----------------------------------------------------------------------


def _definition(hub_dir, workflow: str):
    from . import runtime

    return runtime.definition(hub_dir, workflow)


def _can(hub_dir, actor: str, hub: str, permission: str) -> bool:
    from ..access import store_for

    try:
        return bool(store_for(Path(hub_dir)).can(actor, hub, permission))
    except Exception:  # noqa: BLE001 — fail closed
        log.exception("schedules: access check failed")
        return False


def _validated(hub_dir, hub: str, actor: str, workflow: str, values, cadence: str, tz: str,
               *, check_limits: bool = True, ignore: str | None = None):
    """(definition, inputs dict, cron) for a schedule `actor` may save, or
    ScheduleError with the copy-deck sentence."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from . import inputs as inputs_lib
    from .schedule_grammar import ScheduleError as GrammarError
    from .schedule_grammar import shortest_gap, to_cron

    wf = _definition(hub_dir, workflow)
    if wf is None or not wf.as_tool:
        raise ScheduleError(f"There is no report named {workflow!r} here.")
    if not wf.schedulable:
        raise ScheduleError(f"{wf.title} can't be scheduled. You can still run it any time.")
    if not _can(hub_dir, actor, hub, wf.permission):
        raise ScheduleError(f"You can't run {wf.title} yet. An admin can give it to you in the Console.")
    if not _can(hub_dir, actor, hub, SCHEDULE_PERMISSION):
        raise ScheduleError("You can run this report but not schedule it. An admin can allow "
                            "scheduling in the Console.")
    try:
        data = inputs_lib.dump(inputs_lib.validate(wf.inputs, values or None)) or {}
    except inputs_lib.InputError as exc:
        raise ScheduleError(str(exc).replace("Nothing ran.", "Nothing was saved.")) from None
    try:
        cron = to_cron(cadence)
    except GrammarError:
        raise ScheduleError(f'I can\'t read "{cadence}" as a schedule. Try "every monday at 09:00", '
                            '"daily at 18:00" or "every 2 hours".') from None
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"{tz} isn't a time zone I know. Try a city zone such as "
                            "Asia/Kolkata.") from None
    limits = settings(hub_dir)
    gap = shortest_gap(cadence, tz)
    # A daily limit still allows "daily" on the 23-hour day of a clock change.
    too_often = lambda limit: gap < limit - (3600 if limit >= 86400 else 0)  # noqa: E731
    if wf.min_interval and too_often(wf.min_interval) and wf.min_interval > limits["min_interval"]:
        raise ScheduleError(f'{wf.title} can run at most once every {_span(wf.min_interval)}. '
                            'Try "daily at 09:00".')
    if too_often(limits["min_interval"]):
        raise ScheduleError(f"Personal schedules run at most once every {_span(limits['min_interval'])}. "
                            'Try "every 2 hours" or "daily at 09:00".')
    if check_limits:
        mine = [s for s in for_owner(hub_dir, hub, actor) if s.id != ignore]
        if len(mine) >= limits["per_person"]:
            raise ScheduleError(f"You have {limits['per_person']} schedules on this agent, the most "
                                "allowed. Stop one, then try again.")
        for s in mine:
            if (s.workflow == workflow and s.values == data and s.timezone == tz
                    and to_cron(s.cadence) == cron):
                raise ScheduleError(f'You already have "{s.name}" with the same report, inputs '
                                    "and time. Want to change it instead?")
    return wf, data, cron


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds % 86400 == 0:
        n = seconds // 86400
        return "day" if n == 1 else f"{n} days"
    if seconds % 3600 == 0:
        n = seconds // 3600
        return "hour" if n == 1 else f"{n} hours"
    n = max(1, seconds // 60)
    return "minute" if n == 1 else f"{n} minutes"


def _next_runs(cadence: str, tz: str, count: int = 3, after: float | None = None) -> list[float]:
    from .schedule_grammar import next_after

    cursor = datetime.fromtimestamp(after or time.time(), tz=timezone.utc)
    out = []
    for _ in range(count):
        cursor = next_after(cadence, tz, cursor)
        out.append(cursor.timestamp())
    return out


# ---- proposals --------------------------------------------------------------------


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


def propose(hub_dir, hub: str, actor: str, kind: str, payload: dict) -> str:
    """Store the first half of a two-call action. Returns its code."""
    from .._request_ctx import get_turn_id

    now = time.time()
    code = _new_code()
    same = json.dumps(payload, sort_keys=True, default=str)
    with _engine(hub_dir).begin() as c:
        # Old proposals have no use after a day (an expired one still says so until then).
        c.execute(text("DELETE FROM hz_workflow_proposals WHERE expires<=:t"), {"t": now - 86400})
        # The same proposal again (a model re-proposing after the yes) keeps its
        # code, so the yes the person gave to it still counts.
        for pid, raw in c.execute(text(
                "SELECT id, payload FROM hz_workflow_proposals WHERE hub=:h AND actor=:a AND kind=:k "
                "AND status='pending' AND expires>:t"), {"h": hub, "a": actor, "k": kind, "t": now}):
            earlier = json.loads(raw)
            earlier.pop("_turn", None)
            if json.dumps(earlier, sort_keys=True, default=str) == same:
                return pid
        payload = dict(payload, _turn=get_turn_id())
        c.execute(text("INSERT INTO hz_workflow_proposals (id, hub, actor, kind, payload, created, "
                       "expires, status) VALUES (:i, :h, :a, :k, :p, :t, :e, 'pending')"),
                  {"i": code, "h": hub, "a": actor, "k": kind, "p": json.dumps(payload),
                   "t": now, "e": now + PROPOSAL_SECONDS})
    return code


def take(hub_dir, hub: str, actor: str, code: str, *, kinds: tuple[str, ...]) -> tuple[str, dict]:
    """Use a pending proposal exactly once: (kind, payload), or ScheduleError.
    Never in the chat turn that made it."""
    from .._request_ctx import get_turn_id

    code = (code or "").strip().upper()
    now = time.time()
    with _engine(hub_dir).begin() as c:
        row = c.execute(text("SELECT kind, payload, expires, status FROM hz_workflow_proposals "
                             "WHERE id=:i AND hub=:h AND actor=:a"),
                        {"i": code, "h": hub, "a": actor}).fetchone()
        if row is None or row[3] != "pending" or row[0] not in kinds:
            raise ScheduleError("That confirmation doesn't match a pending proposal. Nothing was saved.")
        if row[2] <= now:
            c.execute(text("UPDATE hz_workflow_proposals SET status='expired' WHERE id=:i"), {"i": code})
            raise ScheduleError("That proposal expired after 10 minutes, so nothing was saved. "
                                "Ask again to get a new one.")
        payload = json.loads(row[1])
        turn = payload.pop("_turn", None)
        if turn is not None and turn == get_turn_id():
            # The yes has to come from the person, in a later message.
            raise ScheduleError("Ask the person first, and confirm only after they say yes in "
                                "their next message. Nothing was saved.")
        done = c.execute(text("UPDATE hz_workflow_proposals SET status='used' WHERE id=:i AND "
                              "status='pending'"), {"i": code})
        if done.rowcount != 1:
            raise ScheduleError("That confirmation was already used. Nothing changed.")
    return row[0], payload


def propose_create(hub_dir, hub: str, actor: str, *, workflow: str, inputs, cadence: str,
                   timezone_name: str, name: str | None = None) -> dict:
    from . import inputs as inputs_lib

    wf, data, _ = _validated(hub_dir, hub, actor, workflow, inputs, cadence, timezone_name)
    shown = inputs_lib.display(wf.inputs, data)
    label = " ".join((name or "").split())[:80] or (f"{wf.title}: {shown}" if shown else wf.title)[:80]
    runs = _next_runs(cadence, timezone_name)
    code = propose(hub_dir, hub, actor, "create", {
        "workflow": workflow, "inputs": data, "cadence": cadence, "timezone": timezone_name,
        "name": label})
    words = cadence_words(cadence)
    what = f"{wf.title} for {shown}" if shown else wf.title
    mail = ", and email you when it changes or fails" if email_on(hub_dir) else ""
    text_ = (f"Not saved yet. {words} ({timezone_name}), I'll run {what} as you{mail}. "
             f"Next runs: {', '.join(_when(t, timezone_name) for t in runs)}. Save it?")
    return {"code": code, "text": text_, "name": label}


def propose_change(hub_dir, hub: str, actor: str, schedule_id: str, *, cadence: str | None,
                   timezone_name: str | None) -> dict:
    s = _own(hub_dir, hub, actor, schedule_id)
    tz = timezone_name or s.timezone
    new_cadence = cadence or s.cadence
    _validated(hub_dir, hub, actor, s.workflow, s.values, new_cadence, tz, ignore=s.id)
    code = propose(hub_dir, hub, actor, "change", {"id": s.id, "cadence": new_cadence, "timezone": tz})
    return {"code": code, "text": (f'Not changed yet. "{s.name}" would run {cadence_words(new_cadence)} '
                                   f"({tz}) instead of {cadence_words(s)} ({s.timezone}). Change it?")}


def propose_stop(hub_dir, hub: str, actor: str, schedule_id: str) -> dict:
    s = _own(hub_dir, hub, actor, schedule_id)
    code = propose(hub_dir, hub, actor, "stop", {"id": s.id})
    return {"code": code, "text": f'Stop "{s.name}" for good? Its live link keeps the last copy. '
                                  "Say yes to stop it."}


def _own(hub_dir, hub: str, actor: str, schedule_id: str) -> Schedule:
    s = find(hub_dir, hub, actor, schedule_id)
    if s is None:
        raise ScheduleError("You have no schedule by that name on this agent. Ask me what's "
                            "scheduled for you.")
    return s


def find(hub_dir, hub: str, actor: str, ref: str) -> Schedule | None:
    """One of `actor`'s schedules by id or by name (case-insensitive)."""
    ref = (ref or "").strip()
    mine = for_owner(hub_dir, hub, actor)
    return next((s for s in mine if s.id == ref), None) or next(
        (s for s in mine if s.name.lower() == ref.lower()), None)


def confirm(hub_dir, hub: str, actor: str, code: str) -> dict:
    """Carry out a confirmed proposal. Returns {kind, schedule, run_id?}."""
    kind, payload = take(hub_dir, hub, actor, code, kinds=("create", "change", "stop"))
    if kind == "create":
        return create(hub_dir, hub, actor, **payload)
    s = _own(hub_dir, hub, actor, payload["id"])
    if kind == "stop":
        _update(hub_dir, s.id, state="stopped", reason="owner", paused_by=actor, next_fire=None)
        return {"kind": "stop", "schedule": get(hub_dir, s.id)}
    _validated(hub_dir, hub, actor, s.workflow, s.values, payload["cadence"], payload["timezone"],
               ignore=s.id)
    nxt = _next_runs(payload["cadence"], payload["timezone"], 1)[0]
    _update(hub_dir, s.id, cadence=payload["cadence"], timezone=payload["timezone"],
            next_fire=nxt if s.state in ("active", "testing") else s.next_fire)
    return {"kind": "change", "schedule": get(hub_dir, s.id)}


def create(hub_dir, hub: str, actor: str, *, workflow: str, inputs: dict, cadence: str,
           timezone: str, name: str) -> dict:
    """Save a confirmed schedule in its testing state and start its first run."""
    from ..access import store_for

    wf, data, _ = _validated(hub_dir, hub, actor, workflow, inputs, cadence, timezone)
    account = (store_for(Path(hub_dir)).identity(actor) or {}).get("owui_id")
    now = time.time()
    schedule_id = "ps" + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    with _engine(hub_dir).begin() as c:
        c.execute(text(
            f"INSERT INTO hz_personal_schedules ({_COLS}) VALUES (:id, :hub, :workflow, :owner, "
            ":owner_account, :name, :inputs, :cadence, :timezone, 'testing', NULL, NULL, :next_fire, "
            "NULL, NULL, NULL, NULL, NULL, 0, :t, :t)"),
            {"id": schedule_id, "hub": hub, "workflow": workflow, "owner": actor,
             "owner_account": account, "name": name, "inputs": json.dumps(data),
             "cadence": cadence, "timezone": timezone,
             "next_fire": _next_runs(cadence, timezone, 1)[0], "t": now})
    sched = get(hub_dir, schedule_id)
    try:
        run_id = start(hub_dir, hub, sched, slot="first")
    except Exception as exc:
        # Not saved after all: a schedule nobody heard about must not run later.
        with _engine(hub_dir).begin() as c:
            c.execute(text("DELETE FROM hz_personal_schedules WHERE id=:id"), {"id": schedule_id})
        if isinstance(exc, ScheduleError):
            raise ScheduleError("Reports can't run on this agent right now, so nothing was saved. "
                                "Ask again later.") from None
        raise
    notify(hub_dir, hub, sched, "created")
    return {"kind": "create", "schedule": get(hub_dir, schedule_id), "run_id": run_id}


def set_paused(hub_dir, hub: str, actor: str, schedule_id: str, paused: bool, *,
               admin: bool = False) -> Schedule:
    """Pause or resume. An owner resumes their own pause and any pause the
    system made; only an admin resumes an admin's pause."""
    s = get(hub_dir, schedule_id) if admin else _own(hub_dir, hub, actor, schedule_id)
    if s is None or s.hub != hub or s.state == "stopped":
        raise ScheduleError("There is no such schedule on this agent.")
    if paused:
        _update(hub_dir, s.id, state="paused", reason="admin" if admin else "owner", paused_by=actor)
    else:
        if s.state != "paused":
            return s
        if s.reason == "admin" and not admin:
            raise ScheduleError(f'"{s.name}" was paused by an admin. Only they can resume it.')
        if admin and s.reason != "admin":
            raise ScheduleError("Admins can resume only schedules they paused.")
        _validated(hub_dir, hub, s.owner, s.workflow, s.values, s.cadence, s.timezone,
                   check_limits=False)
        _update(hub_dir, s.id, state="active", reason=None, paused_by=None, failures=0,
                next_fire=_next_runs(s.cadence, s.timezone, 1)[0])
    if admin and paused:
        notify(hub_dir, hub, get(hub_dir, s.id), "admin_paused")
    return get(hub_dir, s.id)


def admin_stop(hub_dir, hub: str, actor: str, schedule_id: str) -> Schedule:
    s = get(hub_dir, schedule_id)
    if s is None or s.hub != hub or s.state == "stopped":
        raise ScheduleError("There is no such schedule on this agent.")
    _update(hub_dir, s.id, state="stopped", reason="admin", paused_by=actor, next_fire=None)
    notify(hub_dir, hub, get(hub_dir, s.id), "admin_stopped")
    return get(hub_dir, s.id)


def run_now(hub_dir, hub: str, actor: str, schedule_id: str) -> str:
    s = _own(hub_dir, hub, actor, schedule_id)
    if s.state == "paused" and s.reason == "admin":
        raise ScheduleError(f'"{s.name}" was paused by an admin. Only they can resume it.')
    _validated(hub_dir, hub, actor, s.workflow, s.values, s.cadence, s.timezone, check_limits=False)
    # One run per five minutes: asking again joins the run already queued.
    return start(hub_dir, hub, s, slot=f"now-{int(time.time()) // 300}")


# ---- running ----------------------------------------------------------------------


def envelope(s: Schedule, wf=None) -> dict:
    from . import inputs as inputs_lib

    return {"trigger": "personal_schedule", "scope": "personal", "acts_as": s.owner,
            "acts_as_account": s.owner_account, "schedule_id": s.id, "inputs": s.values,
            "inputs_display": inputs_lib.display(getattr(wf, "inputs", None), s.values)}


def start(hub_dir, hub: str, s: Schedule, *, slot: str) -> str:
    """Queue one run of `s` with a run id fixed by its slot."""
    from . import runtime

    wf = runtime._REGISTRY.get(s.workflow)
    if wf is None:
        raise ScheduleError("Reports can't run on this agent right now. Try again later.")
    run_id = f"ps-{s.id}-{slot}"
    runtime.start_run(s.workflow, envelope(s, wf), workflow_id=run_id)
    _update(hub_dir, s.id, last_run=run_id, last_status="running")
    return run_id


def problem(hub_dir, hub: str, s: Schedule) -> str | None:
    """Why `s` can't run now, as a pause reason, or None."""
    from ..access import store_for
    from . import inputs as inputs_lib
    from . import runtime
    from .identity import IdentityError, check

    wf = runtime._REGISTRY.get(s.workflow)
    if wf is None or not wf.schedulable:
        return "removed"
    try:
        ident = check(Path(hub_dir), hub, s.owner, "schedule", account_id=s.owner_account)
    except IdentityError:
        return "access"
    gs = store_for(Path(hub_dir))
    if not gs.can(ident.subject, hub, wf.permission) or not gs.can(ident.subject, hub, SCHEDULE_PERMISSION):
        return "access"
    try:
        inputs_lib.validate(wf.inputs, s.values or None)
    except inputs_lib.InputError:
        return "inputs"
    return None


def fire_due(hub_dir, hub: str, now: float | None = None) -> list[str]:
    """The dispatcher's pass over personal schedules. Returns run ids started."""
    from .schedule_grammar import next_after

    now = now or time.time()
    with _engine(hub_dir).connect() as c:
        due = [Schedule(**dict(r._mapping)) for r in c.execute(text(
            f"SELECT {_COLS} FROM hz_personal_schedules WHERE hub=:h AND state IN "
            "('active', 'testing') AND next_fire IS NOT NULL AND next_fire<=:t ORDER BY next_fire"),
            {"h": hub, "t": now}).fetchall()]
    started = []
    for s in due:
        try:
            reason = problem(hub_dir, hub, s)
            if reason:
                pause(hub_dir, hub, s, reason)
                continue
            # Catch up at most once: the latest slot due, skipped when the next
            # one is close anyway.
            at = datetime.fromtimestamp(now, tz=timezone.utc)
            slot = s.next_fire
            cursor = datetime.fromtimestamp(slot, tz=timezone.utc)
            while True:
                following = next_after(s.cadence, s.timezone, cursor)
                if following.timestamp() > now:
                    break
                cursor = following
            slot = cursor.timestamp()
            nxt = next_after(s.cadence, s.timezone, at).timestamp()
            gap = nxt - slot
            late = now - slot
            fire = not (late > 120 and (nxt - now) < 0.25 * gap)
            run_id = f"ps-{s.id}-{int(slot)}"
            # Claim the slot first, and only while the schedule is still on: a
            # pause or stop that lands meanwhile wins, and two dispatchers never
            # both start it.
            with _engine(hub_dir).begin() as c:
                moved = c.execute(text(
                    "UPDATE hz_personal_schedules SET next_fire=:n, last_fire=:f, updated=:t"
                    + (", last_run=:r, last_status='running'" if fire else "")
                    + " WHERE id=:id AND next_fire=:old AND state IN ('active', 'testing')"),
                    {"n": nxt, "f": slot, "t": time.time(), "id": s.id, "old": s.next_fire,
                     **({"r": run_id} if fire else {})})
            if not fire or moved.rowcount != 1:
                continue
            from . import runtime

            try:
                runtime.start_run(s.workflow, envelope(s, runtime._REGISTRY.get(s.workflow)),
                                  workflow_id=run_id)
            except Exception:  # noqa: BLE001 — the slot is spent; the next one runs
                _update(hub_dir, s.id, last_status="failed", last_cause="error", last_at=time.time())
                raise
            started.append(run_id)
        except Exception:  # noqa: BLE001 — one schedule never stalls the rest
            log.exception("schedules: could not fire %s", s.id)
    return started


def pause(hub_dir, hub: str, s: Schedule, reason: str) -> None:
    _update(hub_dir, s.id, state="paused", reason=reason, paused_by="system")
    notify(hub_dir, hub, get(hub_dir, s.id), "paused")


def after_run(hub_dir, hub: str, env: dict, run_id: str, *, ok: bool, cause: str | None,
              published: list) -> None:
    """Bookkeeping when a personal schedule's run ends. Idempotent per run."""
    s = get(hub_dir, env.get("schedule_id"))
    if s is None or s.state == "stopped":
        return
    if s.last_run == run_id and s.last_status in ("updated", "no_change", "failed"):
        return
    now = time.time()
    if ok:
        changed = any(p.get("changed") for p in published)
        _update(hub_dir, s.id, last_run=run_id, last_status="updated" if changed else "no_change",
                last_at=now, last_cause=None, failures=0,
                **({"state": "active"} if s.state == "testing" else {}))
        if changed and s.state != "testing":
            notify(hub_dir, hub, get(hub_dir, s.id), "changed", published=published)
        return
    failures = s.failures + 1
    limits = settings(hub_dir)
    values = dict(last_run=run_id, last_status="failed", last_at=now, last_cause=cause,
                  failures=failures)
    if s.state == "testing":
        _update(hub_dir, s.id, state="paused", reason="first_run", paused_by="system", **values)
        notify(hub_dir, hub, get(hub_dir, s.id), "paused")
    elif failures >= limits["pause_after_failures"]:
        _update(hub_dir, s.id, state="paused", reason="failures", paused_by="system", **values)
        notify(hub_dir, hub, get(hub_dir, s.id), "paused")
    else:
        _update(hub_dir, s.id, **values)
        if failures == 1:
            notify(hub_dir, hub, get(hub_dir, s.id), "failed")


# ---- notices ----------------------------------------------------------------------


def manage_url(hub_dir, s: Schedule) -> str:
    from ..artifacts import public_base_url

    return f"{public_base_url(hub_dir)}/portal/my/schedules/{s.id}"


def notify(hub_dir, hub: str, s: Schedule | None, kind: str, *, published: list | None = None) -> None:
    """Email the schedule's owner when the hub turned it on (`email_on`).
    Best effort."""
    if s is None or not email_on(hub_dir):
        return
    from .. import email_delivery
    from .identity import RunIdentity
    from .outcomes import CAUSES, links_text
    from .runtime import definition as runtime_definition, title_for

    report = title_for(s.workflow)
    from . import inputs as inputs_lib

    wf = runtime_definition(hub_dir, s.workflow)
    shown = inputs_lib.display(getattr(wf, "inputs", None), s.values)
    if kind == "created":
        subject = f"New schedule: {s.name}"
        body = (f"You saved a schedule in chat: {report}{' for ' + shown if shown else ''}, "
                f"{cadence_words(s)} ({s.timezone}).\n\n"
                f"Didn't mean to? Stop it here (you'll sign in): {manage_url(hub_dir, s)}\n")
    elif kind == "changed":
        subject = f"{s.name}: new report"
        body = (f"Your scheduled report ran and changed.\n\n{links_text(published or [])}\n\n"
                f'Manage your schedules in chat: "my schedules".\n')
    elif kind == "failed":
        subject = f"{s.name}: run failed"
        body = (f"Your scheduled report stopped: {CAUSES.get(s.last_cause or '', CAUSES['error'])}. "
                "Its link still shows the newest copy.\n\n"
                f"If the next {settings(hub_dir)['pause_after_failures'] - 1} runs also fail, "
                "the schedule pauses.\n")
    elif kind == "paused":
        subject = f"{s.name}: paused"
        why = {"failures": f"it failed {settings(hub_dir)['pause_after_failures']} times in a row",
               "first_run": "its first run failed", "access": "your access changed",
               "inputs": "its inputs no longer fit the report",
               "removed": "the report was removed"}.get(s.reason or "", "it can't run")
        body = (f'"{s.name}" is paused because {why}. '
                f'Say "resume {s.name}" in chat when it\'s fixed.\n')
    elif kind == "admin_paused":
        subject = f"{s.name}: paused by an admin"
        body = f'An admin paused your schedule "{s.name}". Only they can resume it.\n'
    elif kind == "admin_stopped":
        subject = f"{s.name}: stopped by an admin"
        body = (f'An admin stopped your schedule "{s.name}". Its live link keeps the last copy. '
                "Ask them if you need it back.\n")
    else:
        return
    try:
        email_delivery.send_to_owner(
            Path(hub_dir), hub=hub, identity=RunIdentity(s.owner, s.owner_account, "schedule", s.owner),
            subject=subject, body=body, workflow=s.workflow, run_id=s.last_run,
            idem_key=f"ps:{s.id}:{kind}:{s.last_run or ''}:{int(s.updated)}")
    except Exception:  # noqa: BLE001 — a notice never breaks a schedule
        log.exception("schedules: could not email %s about %s", s.owner, s.id)
