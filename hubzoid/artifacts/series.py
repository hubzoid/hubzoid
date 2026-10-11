# Hubzoid published artifacts. Apache-2.0 licensed like the rest of the repository.
"""Report series: one live link per report a workflow keeps publishing.

A keyed publish belongs to a series. A team run writes the team series, which
belongs to the agent (`owner` ''), so a blocked or replaced reports account
stops new copies without breaking the link. Run for me and personal schedules
write the person's own series. A key belongs to one workflow in a hub.

Every publish records a touch, even when nothing changed. After a declared
schedule run (or a personal schedule run) finishes, series it no longer
touched read "No longer updating", so an event page that closed says so
without any close call. A failed scheduled run marks its series "not updated
since"; the newest copy stays.

A series may have a company link (`/portal/r/<token>`): an unguessable token stored as
a hash, plus the token encrypted so an admin can copy it again. Replacing it
ends the old link at once; turning it off ends it until it is turned on.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

log = logging.getLogger("hubzoid.artifacts")

TEAM, PERSONAL = "team", "personal"
#: Triggers whose finished run defines which series are still live.
_DEFINING_TRIGGERS = ("schedule", "personal_schedule")
UPDATE_COOLDOWN = 300


@dataclass(frozen=True)
class Series:
    id: str
    hub: str
    workflow: str
    scope: str
    owner: str
    owner_ref: str
    key: str
    title: str | None
    audience: str
    schedule_id: str | None
    last_artifact: str | None
    last_sha: str | None
    last_digest: str | None
    last_inputs: str | None
    last_run: str | None
    last_touch: float | None
    updated_at: float | None
    live: int
    failed_at: float | None
    domain_hash: str | None
    domain_token_enc: str | None
    domain_on: int
    domain_by: str | None
    domain_changed: float | None
    update_requested: float | None
    created: float

    @property
    def team(self) -> bool:
        return self.scope == TEAM

    @property
    def inputs(self) -> dict | None:
        try:
            return json.loads(self.last_inputs) if self.last_inputs else None
        except ValueError:
            return None


_COLS = ", ".join(Series.__dataclass_fields__)


def _engine(hub_dir):
    from . import _engine as eng

    return eng(hub_dir)


def owner_ref(subject: str) -> str:
    """A short, stable reference to a person for their personal links, so a
    link names nobody: /portal/latest/<hub>/<key>/by/<ref>."""
    return hashlib.sha256(("series:" + (subject or "")).encode()).hexdigest()[:12]


def _one(hub_dir, where: str, params: dict) -> Series | None:
    with _engine(hub_dir).connect() as c:
        r = c.execute(text(f"SELECT {_COLS} FROM hz_report_series WHERE {where}"), params).fetchone()
    return Series(**dict(r._mapping)) if r is not None else None


def _many(hub_dir, where: str, params: dict, order: str = "updated_at DESC") -> list[Series]:
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text(f"SELECT {_COLS} FROM hz_report_series WHERE {where} ORDER BY {order}"),
                         params).fetchall()
    return [Series(**dict(r._mapping)) for r in rows]


def get(hub_dir, series_id: str | None) -> Series | None:
    if not series_id:
        return None
    return _one(hub_dir, "id=:i", {"i": series_id})


def team(hub_dir, hub: str, key: str) -> Series | None:
    return _one(hub_dir, "hub=:h AND key=:k AND scope='team'", {"h": hub, "k": key})


def personal(hub_dir, hub: str, key: str, ref: str) -> Series | None:
    return _one(hub_dir, "hub=:h AND key=:k AND scope='personal' AND owner_ref=:r",
                {"h": hub, "k": key, "r": ref})


def for_hub(hub_dir, hub: str) -> list[Series]:
    return _many(hub_dir, "hub=:h AND last_artifact IS NOT NULL", {"h": hub},
                 order="scope DESC, updated_at DESC")


def for_owner(hub_dir, hub: str, owner: str) -> list[Series]:
    return _many(hub_dir, "hub=:h AND scope='personal' AND owner=:o AND last_artifact IS NOT NULL",
                 {"h": hub, "o": owner})


def team_series(hub_dir, hub: str) -> list[Series]:
    return _many(hub_dir, "hub=:h AND scope='team' AND last_artifact IS NOT NULL", {"h": hub})


def touched_by(hub_dir, hub: str, run_id: str) -> list[Series]:
    return _many(hub_dir, "hub=:h AND last_run=:r", {"h": hub, "r": run_id}, order="key")


def _token_hash(token: str) -> str:
    return hashlib.sha256(("domain:" + token).encode()).hexdigest()


def for_token(hub_dir, token: str) -> Series | None:
    if not isinstance(token, str) or not (12 <= len(token) <= 64):
        return None
    return _one(hub_dir, "domain_hash=:h", {"h": _token_hash(token)})


# ---- writing ----------------------------------------------------------------------


def claim(hub_dir, *, hub: str, workflow: str, scope: str, owner: str, key: str,
          title: str | None) -> Series:
    """The series a publish goes to, created on first use. A key that another
    workflow already uses in this hub is refused."""
    from . import ArtifactError

    owner = owner if scope == PERSONAL else ""
    with _engine(hub_dir).connect() as c:
        other = c.execute(text("SELECT workflow FROM hz_report_series WHERE hub=:h AND key=:k "
                               "AND workflow<>:w LIMIT 1"),
                          {"h": hub, "k": key, "w": workflow}).fetchone()
    if other is not None:
        raise ArtifactError(409, f"Key {key} belongs to workflow {other[0]}. Use another key.")
    where = "hub=:h AND workflow=:w AND scope=:s AND owner=:o AND key=:k"
    params = {"h": hub, "w": workflow, "s": scope, "o": owner, "k": key}
    found = _one(hub_dir, where, params)
    if found is not None:
        return found
    try:
        with _engine(hub_dir).begin() as c:
            c.execute(text(
                "INSERT INTO hz_report_series (id, hub, workflow, scope, owner, owner_ref, key, "
                "title, audience, live, domain_on, created) VALUES (:id, :h, :w, :s, :o, :r, :k, "
                ":t, 'owner', 1, 0, :now)"),
                dict(params, id="s" + secrets.token_urlsafe(12), r=owner_ref(owner) if owner else "",
                     t=(title or key)[:300], now=time.time()))
    except IntegrityError:
        pass  # another publish created it first
    found = _one(hub_dir, where, params)
    if found is None:   # another workflow took the key at the same moment
        raise ArtifactError(409, f"Key {key} belongs to another workflow. Use another key.")
    return found


def touch(hub_dir, series: Series, *, run_id: str | None, inputs, schedule_id: str | None,
          audience: str | None = None) -> None:
    """A publish that stored nothing new still counts: this run kept the
    series current."""
    with _engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_report_series SET last_run=:r, last_touch=:t, live=1, "
                       "last_inputs=:i, schedule_id=COALESCE(:sid, schedule_id), "
                       "audience=COALESCE(:au, audience) WHERE id=:id"),
                  {"r": run_id, "t": time.time(), "i": json.dumps(inputs) if inputs is not None else None,
                   "sid": schedule_id, "au": audience, "id": series.id})


def record_copy(hub_dir, series: Series, *, artifact_id: str, sha: str, digest: str | None,
                run_id: str | None, inputs, schedule_id: str | None, title: str | None,
                audience: str) -> None:
    now = time.time()
    with _engine(hub_dir).begin() as c:
        c.execute(text(
            "UPDATE hz_report_series SET last_artifact=:a, last_sha=:sha, last_digest=:d, "
            "last_run=:r, last_touch=:t, updated_at=:t, live=1, last_inputs=:i, title=:title, "
            "audience=:au, schedule_id=COALESCE(:sid, schedule_id) WHERE id=:id"),
            {"a": artifact_id, "sha": sha, "d": digest, "r": run_id, "t": now,
             "i": json.dumps(inputs) if inputs is not None else None, "title": (title or series.key)[:300],
             "au": audience, "sid": schedule_id, "id": series.id})


def finish_run(hub_dir, hub: str, workflow: str, run_id: str, *, trigger: str | None,
               scope: str | None, schedule_id: str | None, ok: bool) -> None:
    """After a run: which series are live, and which failed to update. Only a
    declared schedule or a personal schedule decides that a series it no longer
    touches has stopped; a one-off run never does."""
    if trigger not in _DEFINING_TRIGGERS:
        return
    if trigger == "schedule" and scope == TEAM:
        where, params = "hub=:h AND workflow=:w AND scope='team'", {"h": hub, "w": workflow}
    elif trigger == "personal_schedule" and schedule_id:
        where, params = "hub=:h AND schedule_id=:sid", {"h": hub, "sid": schedule_id}
    else:
        return
    with _engine(hub_dir).begin() as c:
        if ok:
            c.execute(text(f"UPDATE hz_report_series SET live=CASE WHEN last_run=:r THEN 1 ELSE 0 END, "
                           f"failed_at=CASE WHEN last_run=:r THEN NULL ELSE failed_at END WHERE {where}"),
                      dict(params, r=run_id))
        else:
            c.execute(text(f"UPDATE hz_report_series SET failed_at=COALESCE(failed_at, :t) "
                           f"WHERE {where} AND live=1"), dict(params, t=time.time()))


def prune(hub_dir, series: Series, keep: int) -> int:
    """Keep only the newest `keep` copies of a series (a hub's `series_keep`).
    Older copies are deleted like any artifact. Returns how many went."""
    from . import _remove_files

    if not keep or keep < 1:
        return 0
    with _engine(hub_dir).connect() as c:
        ids = [r[0] for r in c.execute(text(
            "SELECT id FROM hz_artifacts WHERE series_id=:s AND deleted IS NULL "
            "ORDER BY created DESC, id DESC"), {"s": series.id}).fetchall()]
    old = ids[keep:]
    now = time.time()
    for artifact_id in old:
        with _engine(hub_dir).begin() as c:
            c.execute(text("UPDATE hz_artifact_links SET revoked=:t WHERE artifact_id=:a "
                           "AND revoked IS NULL"), {"a": artifact_id, "t": now})
            c.execute(text("DELETE FROM hz_artifact_shares WHERE artifact_id=:a"), {"a": artifact_id})
            c.execute(text("UPDATE hz_artifacts SET deleted=:t, updated=:t WHERE id=:a"),
                      {"t": now, "a": artifact_id})
        _remove_files(hub_dir, artifact_id)
    return len(old)


# ---- links ------------------------------------------------------------------------


def latest_path(series: Series) -> str:
    base = f"/portal/latest/{series.hub}/{series.key}"
    return base if series.team else f"{base}/by/{series.owner_ref}"


def latest_url(hub_dir, series: Series) -> str:
    from . import public_base_url

    return public_base_url(hub_dir) + latest_path(series)


def share_url(hub_dir, token: str) -> str:
    from . import public_base_url

    return f"{public_base_url(hub_dir)}/portal/r/{token}"


def domain_token(hub_dir, series: Series) -> str | None:
    """The current company link token, when the link is on."""
    if not series.domain_on or not series.domain_token_enc:
        return None
    from .. import secretbox

    try:
        return secretbox.decrypt_text(Path(hub_dir), series.domain_token_enc)
    except Exception:  # noqa: BLE001 — a key change: the admin replaces the link
        log.exception("artifacts: company link of %s can't be read", series.id)
        return None


def _new_token(hub_dir, series: Series, actor: str) -> str:
    from .. import secretbox

    token = secrets.token_urlsafe(12)
    with _engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_report_series SET domain_hash=:h, domain_token_enc=:e, "
                       "domain_on=1, domain_by=:b, domain_changed=:t WHERE id=:id"),
                  {"h": _token_hash(token), "e": secretbox.encrypt(Path(hub_dir), token),
                   "b": actor, "t": time.time(), "id": series.id})
    return token


def domain_on(hub_dir, series: Series, actor: str) -> str:
    """The company link, turned on if it was off. Turning a link back on gives
    a new one, so people who had the old link don't get access back."""
    return domain_token(hub_dir, series) or _new_token(hub_dir, series, actor)


#: The actor recorded when a team run's code turns its company link on or off.
BY_WORKFLOW = "workflow"


def follow_code(hub_dir, series: Series, wants_link: bool) -> None:
    """A team run says whether its report has a company link. The code turns
    the link on and off, except that a link a person turned off stays off
    until someone makes a new one in the Console."""
    token = domain_token(hub_dir, series)
    if wants_link and not token and series.domain_by in (None, BY_WORKFLOW):
        _new_token(hub_dir, series, BY_WORKFLOW)
    elif not wants_link and token:
        domain_off(hub_dir, series, BY_WORKFLOW)


def reserve_update(hub_dir, series: Series, now: float | None = None) -> bool:
    """Take this series' Update now slot: at most one every UPDATE_COOLDOWN
    seconds, counted from the last copy or the last request."""
    now = now or time.time()
    edge = now - UPDATE_COOLDOWN
    with _engine(hub_dir).begin() as c:
        done = c.execute(text(
            "UPDATE hz_report_series SET update_requested=:t WHERE id=:id "
            "AND (update_requested IS NULL OR update_requested<:e) AND (last_touch IS NULL OR last_touch<:e)"),
            {"t": now, "e": edge, "id": series.id})
    return done.rowcount == 1


def update_wait(series: Series, now: float | None = None) -> int:
    """Seconds until Update now may run again (0 when it may)."""
    now = now or time.time()
    last = max(series.last_touch or 0, series.update_requested or 0)
    return max(0, int(UPDATE_COOLDOWN - (now - last))) if last else 0


def domain_replace(hub_dir, series: Series, actor: str) -> str:
    """A new company link; the old one stops working now."""
    return _new_token(hub_dir, series, actor)


def domain_off(hub_dir, series: Series, actor: str) -> None:
    with _engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_report_series SET domain_on=0, domain_by=:b, domain_changed=:t "
                       "WHERE id=:id"), {"b": actor, "t": time.time(), "id": series.id})


# ---- state for the viewer, chat and Console ---------------------------------------


def next_update(hub_dir, series: Series, *, now: float | None = None) -> float | None:
    """When this series is next expected to update, or None."""
    from datetime import datetime, timezone

    now = now or time.time()
    if not series.live:
        return None
    try:
        if series.team:
            from ..access import store_for
            from ..workflows.observe import definitions
            from ..workflows.schedule_grammar import next_after

            row = next((d for d in definitions(hub_dir) if d["name"] == series.workflow), None)
            if not row or not row.get("schedule") or row.get("error"):
                return None
            if series.workflow in store_for(hub_dir).paused_workflows(series.hub):
                return None
            at = datetime.fromtimestamp(now, tz=timezone.utc)
            return next_after(row["schedule"], row.get("timezone") or "UTC", at).timestamp()
        if series.schedule_id:
            from ..workflows import personal

            sched = personal.get(hub_dir, series.schedule_id)
            if sched and sched.state in ("active", "testing") and sched.next_fire:
                return sched.next_fire
    except Exception:  # noqa: BLE001 — a hint, never an error page
        log.debug("artifacts: next update unknown for %s", series.id, exc_info=True)
    return None


def state(hub_dir, series: Series) -> dict:
    """{state: live | stopped | failed, updated, next, failed_since}."""
    if not series.live:
        kind = "stopped"
    elif series.failed_at:
        kind = "failed"
    else:
        kind = "live"
    return {"state": kind, "updated": series.updated_at, "checked": series.last_touch,
            "next": next_update(hub_dir, series) if kind != "stopped" else None,
            "failed_since": series.failed_at}
