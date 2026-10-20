# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""Who a run is for: one record per run, written before any model or tool runs.

Every chat reply, ``/v1`` API call, MCP tool call, workflow run and scheduled
run gets one row in ``hz_run_identities`` (migration ``op_0032``) when it is
admitted. It is never changed afterwards:

  * ``acts_as``: the account whose access the run uses (id and email);
  * ``requested_by``: who asked (the same person in chat; a schedule's owner;
    an outside sender, for channels);
  * ``started_from``: chat, api, mcp, workflow, schedule or webhook, and the
    conversation, run or schedule id (``origin_id``);
  * ``person_present``: someone is there to answer now (the chat window).
    Schedules, webhooks, API and MCP calls are not. Tools never ask anyone
    for permission; this only tells a request that needs a person (a
    question or an approval) where to ask: in the chat, or the requester's
    pending list;
  * ``credential``: the session, API key or MCP grant that started it, its
    limits (``scope``) and the account's credential counter then
    (``auth_epoch``). Each tool call checks the counter is unchanged
    (``evaluator.check_run``), so revoking credentials stops work in flight;
  * ``parent_id``: the run this one was started from (it can only narrow);
  * ``legacy``: a run from before this record existed, whose owner could not
    be confirmed; it shows as such and gets no extra trust.

Only server-side intake code calls ``admit``. Browser input, model output,
tool arguments and imports never reach it. Admitting the same run twice (a
workflow recovering after a restart) returns the first record unchanged.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from .identity import Identity, normalize

log = logging.getLogger("hubzoid.access")

# Credentials checked again on every tool call (``evaluator.check_run``). An
# MCP call is its own run and its token is checked on each request already.
LIVE_CREDENTIALS = ("session", "api_key")
KINDS = ("chat", "api", "mcp", "workflow", "schedule", "webhook", "eval")
_metadata = sa.MetaData()
records = sa.Table(
    "hz_run_identities", _metadata,
    sa.Column("id", sa.String(40), primary_key=True),
    sa.Column("run_kind", sa.String(16), nullable=False),
    sa.Column("run_id", sa.String(128), nullable=False),
    sa.Column("hub", sa.String(255), nullable=False),
    sa.Column("acts_as", sa.String(64)),
    sa.Column("acts_as_email", sa.String(320)),
    sa.Column("requested_by", sa.String(320)),
    sa.Column("requested_by_kind", sa.String(16), nullable=False),
    sa.Column("started_from", sa.String(16), nullable=False),
    sa.Column("origin_id", sa.String(128)),
    sa.Column("person_present", sa.Integer, nullable=False),
    sa.Column("credential_kind", sa.String(16)),
    sa.Column("credential_id", sa.String(64)),
    sa.Column("scope", sa.Text),
    sa.Column("auth_epoch", sa.Integer),
    sa.Column("parent_id", sa.String(40)),
    sa.Column("legacy", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)


def _engine(hub_dir: Path):
    from ..auth.schema import engine_for

    return engine_for(Path(hub_dir))


def _row(row) -> dict:
    m = dict(row._mapping)
    m["person_present"] = bool(m["person_present"])
    m["legacy"] = bool(m["legacy"])
    m["scope"] = json.loads(m["scope"]) if m["scope"] else None
    return m


def for_run(hub_dir: Path, run_kind: str, run_id: str) -> dict | None:
    with _engine(hub_dir).connect() as conn:
        row = conn.execute(sa.select(records).where(
            records.c.run_kind == run_kind, records.c.run_id == str(run_id))).first()
    return _row(row) if row else None


def get(hub_dir: Path, record_id: str) -> dict | None:
    with _engine(hub_dir).connect() as conn:
        row = conn.execute(sa.select(records).where(records.c.id == str(record_id))).first()
    return _row(row) if row else None


def admit(hub_dir: Path, *, run_kind: str, run_id: str, acts_as_email: str | None,
          acts_as_id: str | None = None, requested_by: str | None = None,
          requested_by_kind: str = "account", started_from: str | None = None,
          origin_id: str | None = None, person_present: bool = False,
          credential_kind: str | None = None, credential_id: str | None = None,
          scope: dict | None = None, parent_id: str | None = None,
          legacy: bool = False) -> dict:
    """Write (once) and return the record for this run. The account is
    resolved here, by id when known, and its credential counter recorded."""
    if run_kind not in KINDS:
        raise ValueError(f"unknown run kind {run_kind!r}")
    found = for_run(hub_dir, run_kind, run_id)
    if found is not None:
        return found
    email = normalize(acts_as_email or "") or None
    epoch = None
    account = None
    if email or acts_as_id:
        from ..auth import users

        st = users.store(Path(hub_dir))
        account = st.get(acts_as_id) if acts_as_id else (st.find_by_email(email) if email else None)
        if account is not None and email and account["email"] != email and not acts_as_id:
            account = None
        if account is not None:
            acts_as_id, epoch = account["id"], int(account.get("auth_epoch") or 0)
            email = email or account["email"]
    row = {
        "id": "ri_" + secrets.token_hex(12), "run_kind": run_kind, "run_id": str(run_id)[:128],
        "hub": normalize(Path(hub_dir).name), "acts_as": acts_as_id, "acts_as_email": email,
        "requested_by": (requested_by or email or "")[:320] or None,
        "requested_by_kind": requested_by_kind, "started_from": started_from or run_kind,
        "origin_id": (origin_id or "")[:128] or None, "person_present": 1 if person_present else 0,
        "credential_kind": credential_kind, "credential_id": (credential_id or "")[:64] or None,
        "scope": json.dumps(scope) if scope else None, "auth_epoch": epoch,
        "parent_id": parent_id, "legacy": 1 if legacy else 0, "created_at": time.time(),
    }
    try:
        with _engine(hub_dir).begin() as conn:
            conn.execute(records.insert().values(**row))
    except IntegrityError:
        again = for_run(hub_dir, run_kind, run_id)
        if again is not None:
            return again
        raise
    return _row_from(row)


def _row_from(values: dict) -> dict:
    out = dict(values)
    out["person_present"] = bool(out["person_present"])
    out["legacy"] = bool(out["legacy"])
    out["scope"] = json.loads(out["scope"]) if out["scope"] else None
    return out


def identity(record: dict, surface: str, groups=None) -> Identity:
    """The request identity for this run: the account, its counter, the record."""
    scope = record.get("scope") or {}
    hubs = scope.get("hubs") if isinstance(scope, dict) else None
    kind, cid = record.get("credential_kind"), record.get("credential_id")
    return Identity.make(record.get("acts_as_email"), groups, surface,
                         account_id=record.get("acts_as"), auth_epoch=record.get("auth_epoch"),
                         run_identity_id=record.get("id"),
                         hubs=tuple(hubs) if hubs is not None else None,
                         credential=(kind, cid) if kind in LIVE_CREDENTIALS and cid else None)


class NotAdmitted(RuntimeError):
    """The run's record could not be written, so the run does not start: a
    run without its record would escape the checks that stop it later."""


def admit_or_refuse(hub_dir: Path, **fields) -> dict:
    """``admit``; when the record can't be written, log it and raise
    ``NotAdmitted`` so the caller refuses the run."""
    try:
        return admit(hub_dir, **fields)
    except Exception as exc:  # noqa: BLE001
        log.warning("access: run identity for %s %s not recorded; refusing the run",
                    fields.get("run_kind"), fields.get("run_id"), exc_info=True)
        raise NotAdmitted("Who this run is for could not be recorded. Try again shortly.") from exc


_CHANNELS = {"whatsapp": "WhatsApp", "telegram": "Telegram", "slack": "Slack"}


def describe(record: dict | None, names: dict | None = None) -> str:
    """Plain words for people: "Started by Priya in chat"."""
    if not record:
        return "Recorded when the work starts."
    if record.get("legacy"):
        return "Older run, from before who-it-is-for was recorded."
    names = names or {}
    actor = names.get(record.get("acts_as_email")) or record.get("acts_as_email") or "an account"
    asked = names.get(record.get("requested_by")) or record.get("requested_by") or actor
    where = {"chat": "in chat", "api": "through the API", "mcp": "from an assistant (MCP)",
             "workflow": "as a workflow", "schedule": "on a schedule",
             "webhook": "from " + _CHANNELS.get(record.get("credential_kind") or "", "a channel"),
             "eval": "as an eval"}.get(record.get("started_from") or "", "")
    if record.get("started_from") == "schedule":
        lead = f"Scheduled run for {asked}, no one watching"
    elif asked == actor:
        lead = f"Started by {asked} {where}".rstrip()
    else:
        lead = f"Runs as {actor}, requested by {asked} {where}".rstrip()
    if not record.get("person_present") and record.get("started_from") not in ("schedule",):
        lead += " (no one watching)"
    return lead
