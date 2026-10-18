"""Usage limits: `budgets.yaml`, checked before every model call.

The file is optional. Without it nothing is limited. Example:

```yaml
version: 1
limits:
  run:          # one chat reply, or every model call of one workflow run
    max_turns: 40
    max_output_tokens: 100000
    max_cost_usd: "2.00"
  work_item:    # every run of one work item
    max_output_tokens: 300000
    max_cost_usd: "5.00"
  month:        # everything this hub does in a UTC calendar month
    max_cost_usd: "40.00"
```

Each scope takes `max_input_tokens`, `max_output_tokens` and `max_cost_usd`;
`run` also takes `max_turns`. Token and turn limits work with subscription
logins. A dollar limit needs a price: Claude reports its own cost, other
models are priced from LiteLLM's table, and a model with no price is refused
while a dollar limit applies (`unpriced_model`), never run unbounded.

`admit` is the one check before a model call (chat replies, workflow calls,
isolated agents): it adds up the recorded usage (`hz_usage`) and the open
reservations of every scope under a lock, refuses with `budget_exhausted` when
a scope has no room left, and otherwise reserves the run's own limit (or
what is left, when less), so calls running at the same time share what
remains. Without a run limit, calls already running when a scope fills up
may each finish. The runtime's own caps are set
from what is left: Claude's `max_turns` and `max_budget_usd`, the turn limit on
Codex and the OpenAI Agents runtime. A reply stopped by them says so, and the
person can reply "continue" to start a new run, which is checked again.
Token limits stop the next call, not one already running.

A reservation that never settles (the process stopped mid-call) keeps holding
its amount and is listed as unknown until an administrator releases it.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from sqlalchemy import text

from .work_items.errors import WorkItemError

FILE = "budgets.yaml"
SCOPES = ("run", "work_item", "month")
_KEYS = ("max_input_tokens", "max_output_tokens", "max_cost_usd")
_RUN_KEYS = _KEYS + ("max_turns",)
_ITEM_RUN = re.compile(r"^wi-([0-9a-f-]{36})-e\d+$")
#: A run stopped by the runtime's own turn or cost cap.
_STOPPED = re.compile(r"error_max_turns|error_max_budget_usd|MaxTurnsExceeded|reached the configured tool-call limit")
RUN_LIMIT = ("This reply stopped at its limit for one run. Reply \"continue\" to keep going, "
             "or leave it here.")
#: The admission of the model call running now, for the runtime adapters.
_CURRENT: ContextVar["Ticket | None"] = ContextVar("hubzoid_budget_ticket", default=None)


class BudgetError(WorkItemError):
    """A model call refused before it started. Nothing was charged."""


@dataclass(frozen=True)
class Config:
    limits: dict = field(default_factory=dict)     # scope -> {key: int | Decimal}
    present: bool = False
    revision: str = "absent"

    def to_dict(self) -> dict:
        return {scope: {k: (str(v) if isinstance(v, Decimal) else v) for k, v in caps.items()}
                for scope, caps in self.limits.items()}


@dataclass
class Ticket:
    id: str | None
    run_id: str
    item_id: str | None
    max_turns: int | None = None
    max_budget_usd: float | None = None


def _fail(source, root, keys, message):
    line = None
    node = root
    for i, key in enumerate(keys):
        if not isinstance(node, yaml.MappingNode):
            break
        match = next(((k, v) for k, v in node.value if getattr(k, "value", None) == key), None)
        if match is None:
            break
        line = match[0].start_mark.line + 1
        node = match[1]
    where = f"{source}:{line}" if line else source
    raise BudgetError("invalid_budgets", f"{where}: {message}", status=422, file=source, line=line)


def parse(text_: str, source: str = FILE) -> Config:
    """Validate the file's text. Errors name the file and line."""
    try:
        root = yaml.compose(text_) if text_.strip() else None
        data = yaml.safe_load(text_) if text_.strip() else {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise BudgetError("invalid_budgets", f"{source}:{mark.line + 1 if mark else '?'}: not valid YAML",
                          status=422, file=source)
    data = data or {}
    if not isinstance(data, dict):
        _fail(source, root, (), "the file must be a mapping")
    for key in data:
        if key not in ("version", "limits"):
            hint = (" Per-person, per-agent and price settings are not supported; use run, work_item "
                    "and month limits." if key in ("allocations", "prices", "defaults", "compaction",
                                                    "currency") else "")
            _fail(source, root, (key,), f"unknown key {key!r}.{hint}")
    if data.get("version") != 1:
        _fail(source, root, ("version",), "version must be 1")
    limits = data.get("limits") or {}
    if not isinstance(limits, dict):
        _fail(source, root, ("limits",), "limits must be a mapping")
    out: dict = {}
    for scope, caps in limits.items():
        if scope not in SCOPES:
            _fail(source, root, ("limits", scope), f"a scope is one of {', '.join(SCOPES)}")
        if not isinstance(caps, dict):
            _fail(source, root, ("limits", scope), f"{scope} must be a mapping")
        allowed = _RUN_KEYS if scope == "run" else _KEYS
        parsed = {}
        for key, value in caps.items():
            if key not in allowed:
                _fail(source, root, ("limits", scope, key), f"{scope} takes {', '.join(allowed)}")
            if key == "max_cost_usd":
                try:
                    amount = Decimal(str(value))
                except InvalidOperation:
                    amount = Decimal(-1)
                if (isinstance(value, bool) or not amount.is_finite() or amount < 0
                        or amount.as_tuple().exponent < -6):
                    _fail(source, root, ("limits", scope, key),
                          "max_cost_usd is an amount such as \"2.00\" (0 or more, at most 6 decimals)")
                parsed[key] = amount
            else:
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    _fail(source, root, ("limits", scope, key), f"{key} is a whole number of 1 or more")
                parsed[key] = value
        if parsed:
            out[scope] = parsed
    revision = hashlib.sha256(text_.encode()).hexdigest()[:16]
    return Config(limits=out, present=True, revision=revision)


def load(hub_dir) -> Config:
    path = Path(hub_dir) / FILE
    if not path.is_file():
        return Config()
    return parse(path.read_text(encoding="utf-8"))


def apply(hub_dir, text_: str, *, expected_revision: str | None, actor: str = "") -> dict:
    """Write budgets.yaml after validating it, only if it is still at
    `expected_revision` ("absent" for a new file)."""
    import os
    import tempfile

    from .work_items import store

    new = parse(text_)
    path = Path(hub_dir) / FILE
    with store.locked(hub_dir, f"budgets-file:{Path(hub_dir).name}"):
        current = load(hub_dir)
        if expected_revision is None or expected_revision != current.revision:
            raise BudgetError("revision_conflict", "budgets.yaml changed since you read it. Reload it, review the "
                              "change and try again.", status=409, current_revision=current.revision)
        fd, tmp = tempfile.mkstemp(prefix=".budgets-", suffix=".yaml", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text_)
        os.replace(tmp, path)
    _audit(hub_dir, actor, "budgets_apply", new.revision)
    return {"revision": new.revision, "previous_revision": current.revision, "limits": new.to_dict()}


# -- usage ------------------------------------------------------------------------

def _engine(hub_dir):
    from .work_items import store

    return store.engine(hub_dir)


def _month_start(now: float | None = None) -> float:
    t = datetime.fromtimestamp(now or time.time(), timezone.utc)
    return datetime(t.year, t.month, 1, tzinfo=timezone.utc).timestamp()


def _month_key(now: float | None = None) -> str:
    return datetime.fromtimestamp(now or time.time(), timezone.utc).strftime("%Y-%m")


def item_of(run_id: str | None) -> str | None:
    match = _ITEM_RUN.match(run_id or "")
    return match.group(1) if match else None


def _used(conn, hub: str, scope: str, key: str, *, run_id=None, item_id=None) -> dict:
    """Recorded usage of a scope: tokens, known cost, and calls with no price."""
    sql = ("SELECT COALESCE(SUM(input_tokens), 0) AS inp, COALESCE(SUM(output_tokens), 0) AS outp, "
           "COALESCE(SUM(cost_usd), 0) AS cost, SUM(CASE WHEN cost_usd IS NULL AND "
           "(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) > 0 THEN 1 ELSE 0 END) AS unpriced "
           "FROM hz_usage WHERE hub=:h")
    args: dict = {"h": hub}
    if scope == "run":
        sql += " AND chat_id=:r"
        args["r"] = run_id
    elif scope == "work_item":
        sql += " AND chat_id LIKE :p"
        args["p"] = f"wi-{item_id}-e%"
    else:
        sql += " AND ts >= :t"
        args["t"] = _month_start()
    row = conn.execute(text(sql), args).mappings().first()
    return {"input_tokens": int(row["inp"] or 0), "output_tokens": int(row["outp"] or 0),
            "cost_usd": Decimal(str(row["cost"] or 0)), "unpriced_calls": int(row["unpriced"] or 0)}


def _reserved(conn, hub: str, scope: str, *, run_id=None, item_id=None) -> dict:
    sql = ("SELECT COALESCE(SUM(input_tokens), 0) AS inp, COALESCE(SUM(output_tokens), 0) AS outp, "
           "COALESCE(SUM(cost_usd), 0) AS cost FROM hz_budget_reservations WHERE hub=:h AND state='open'")
    args: dict = {"h": hub}
    if scope == "run":
        sql += " AND run_id=:r"
        args["r"] = run_id
    elif scope == "work_item":
        sql += " AND item_id=:i"
        args["i"] = item_id
    # For the month: every open hold, also one from last month that never
    # reported, until it settles or an administrator releases it.
    row = conn.execute(text(sql), args).mappings().first()
    return {"input_tokens": int(row["inp"] or 0), "output_tokens": int(row["outp"] or 0),
            "cost_usd": Decimal(str(row["cost"] or 0))}


def priced(model: str | None) -> bool:
    """Whether a call on `model` has a cost figure a dollar limit can count."""
    if not model:
        return False
    name = model.lower()
    if name.startswith("claude-local"):
        return True       # the Claude Agent SDK reports each run's cost
    if name.startswith("codex-local"):
        return False      # no cost figure from a Codex login
    from .usage import estimate_cost

    return estimate_cost(model, 1000, 1000) is not None


_UNITS = {"max_input_tokens": ("input_tokens", "input tokens"),
          "max_output_tokens": ("output_tokens", "output tokens"),
          "max_cost_usd": ("cost_usd", "USD")}
_SCOPE_WORDS = {"run": "this run", "work_item": "this work item", "month": "this hub this month"}


def admit(hub_dir, *, model: str | None, run_id: str | None = None) -> Ticket | None:
    """Check every applicable limit before a model call and reserve the run's
    own limit. Returns None when the hub has no budgets.yaml. Raises
    BudgetError before anything is charged."""
    cfg = load(hub_dir)
    if not cfg.present or not cfg.limits:
        return None
    from .usage import _RUN_TAG

    run_id = run_id or _RUN_TAG.get() or f"reply-{uuid.uuid4()}"
    item_id = item_of(run_id)
    scopes = [s for s in SCOPES if s in cfg.limits and (s != "work_item" or item_id)]
    if any("max_cost_usd" in cfg.limits[s] for s in scopes) and not priced(model):
        raise BudgetError("unpriced_model", f"This hub has a dollar limit, and {model or 'this model'} has no "
                          "price to count against it. Use token or turn limits for this model instead.",
                          status=422, model=model)
    hub = Path(hub_dir).name
    run_caps = cfg.limits.get("run", {})
    from .work_items import store

    remaining: dict = {}        # per unit: (room left, scope that leaves the least)
    with store.locked(hub_dir, f"budgets:{hub}") as conn:
        for scope in scopes:
            used = _used(conn, hub, scope, "", run_id=run_id, item_id=item_id)
            held = _reserved(conn, hub, scope, run_id=run_id, item_id=item_id)
            for key, cap in cfg.limits[scope].items():
                if key == "max_turns":
                    continue
                column, unit = _UNITS[key]
                room = cap - (used[column] + held[column])
                if room <= 0:
                    shown = f"{cap:.2f} USD" if key == "max_cost_usd" else f"{cap:,} {unit}"
                    raise BudgetError(
                        "budget_exhausted",
                        f"The limit of {shown} for {_SCOPE_WORDS[scope]} is used up"
                        + (", so nothing more can run until next month or an administrator raises it in "
                           "budgets.yaml." if scope == "month" else
                           ". An administrator can raise it in budgets.yaml." if scope == "work_item" else
                           ". Start a new run to continue."),
                        status=429, scope=scope, limit=key)
                if key not in remaining or room < remaining[key][0]:
                    remaining[key] = (room, scope)
        # Hold what this call may use: the run's own limit, or less when less
        # is left, so calls running at the same time share what remains.
        reserve = {}
        for key in _KEYS:
            if key in run_caps and key in remaining:
                reserve[key] = min(run_caps[key], remaining[key][0])
        ticket_id = None
        if reserve:
            ticket_id = str(uuid.uuid4())
            conn.execute(text(
                "INSERT INTO hz_budget_reservations (id, hub, run_id, item_id, month, model, input_tokens, "
                "output_tokens, cost_usd, state, created) VALUES (:id, :h, :r, :i, :m, :mo, :inp, :out, :c, "
                "'open', :t)"),
                {"id": ticket_id, "h": hub, "r": run_id, "i": item_id, "m": _month_key(), "mo": model,
                 "inp": int(reserve.get("max_input_tokens", 0)), "out": int(reserve.get("max_output_tokens", 0)),
                 "c": float(reserve.get("max_cost_usd", 0)), "t": time.time()})
    usd = reserve.get("max_cost_usd", remaining.get("max_cost_usd", (None,))[0])
    return Ticket(id=ticket_id, run_id=run_id, item_id=item_id, max_turns=run_caps.get("max_turns"),
                  max_budget_usd=float(usd) if usd is not None and usd > 0 else None)


def settle(hub_dir, ticket: Ticket | None) -> None:
    """The call ended and its usage row is the record now."""
    if ticket is None or ticket.id is None:
        return
    with _engine(hub_dir).begin() as c:
        settle_in(c, ticket.id)


def settle_in(conn, reservation_id: str | None) -> None:
    """Settle in the transaction that writes the call's usage row, so the
    hold and the recorded usage never both disappear."""
    if reservation_id:
        conn.execute(text("UPDATE hz_budget_reservations SET state='settled', settled=:t "
                          "WHERE id=:i AND state='open'"), {"t": time.time(), "i": reservation_id})


def current_id() -> str | None:
    ticket = _CURRENT.get()
    return ticket.id if ticket is not None else None


def release(hub_dir, reservation_id: str, actor: str, *, reason: str) -> dict:
    """An administrator releases a reservation whose call never reported
    (the process stopped mid-call), after checking the provider's usage."""
    if not reason or not reason.strip():
        raise BudgetError("validation_failed", "Say why, for the audit log.", status=422)
    with _engine(hub_dir).begin() as c:
        changed = c.execute(text("UPDATE hz_budget_reservations SET state='released', settled=:t, note=:n "
                                 "WHERE id=:i AND hub=:h AND state='open'"),
                            {"t": time.time(), "n": reason.strip()[:500], "i": reservation_id,
                             "h": Path(hub_dir).name}).rowcount
    if not changed:
        raise BudgetError("not_found", "No open reservation with that id.", status=404)
    _audit(hub_dir, actor, "budget_release", reservation_id)
    return {"id": reservation_id, "state": "released"}


def status(hub_dir) -> dict:
    """Limits, this month's usage, and reservations still open."""
    cfg = load(hub_dir)
    hub = Path(hub_dir).name
    with _engine(hub_dir).connect() as conn:
        month = _used(conn, hub, "month", "")
        held = _reserved(conn, hub, "month")
        rows = conn.execute(text("SELECT * FROM hz_budget_reservations WHERE hub=:h AND state='open' "
                                 "ORDER BY created"), {"h": hub}).mappings().all()
    now = time.time()
    open_rows = [{"id": r["id"], "run_id": r["run_id"], "item_id": r["item_id"], "model": r["model"],
                  "cost_usd": r["cost_usd"], "output_tokens": r["output_tokens"], "created": r["created"],
                  "unknown": now - r["created"] > 3600} for r in rows]
    return {"present": cfg.present, "revision": cfg.revision, "limits": cfg.to_dict(),
            "month": {"key": _month_key(), **{k: (str(v) if isinstance(v, Decimal) else v)
                                              for k, v in month.items()}},
            "reserved": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in held.items()},
            "open_reservations": open_rows}


def work_item_usage(hub_dir, item_id: str) -> dict:
    with _engine(hub_dir).connect() as conn:
        used = _used(conn, Path(hub_dir).name, "work_item", "", item_id=item_id)
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in used.items()}


def _audit(hub_dir, actor, action, subject) -> None:
    from .work_items import service as wi_service

    wi_service._audit(hub_dir, actor or "", action, subject, surface="budgets")


# -- the admission around a model call ----------------------------------------------

def current() -> Ticket | None:
    """The admission of the model call running now, for runtime caps."""
    return _CURRENT.get()


def cap_turns(default: int | None) -> int | None:
    ticket = _CURRENT.get()
    if ticket is None or ticket.max_turns is None:
        return default
    return min(default, ticket.max_turns) if default else ticket.max_turns


class call:
    """`with budgets.call(hub_dir, model=...):` around one model call outside
    a runtime stream (workflow completions, isolated agents). Nested calls
    reuse the outer admission."""

    def __init__(self, hub_dir, *, model: str | None):
        self.hub_dir, self.model = hub_dir, model
        self.ticket = None
        self._token = None

    def __enter__(self):
        if _CURRENT.get() is not None or self.hub_dir is None:
            return _CURRENT.get()
        self.ticket = admit(self.hub_dir, model=self.model)
        self._token = _CURRENT.set(self.ticket)
        return self.ticket

    def __exit__(self, *exc):
        # The hold is settled with the call's usage row (`usage.record`); one
        # whose row was never written stays open, as unknown usage.
        if self._token is not None:
            _CURRENT.reset(self._token)


async def guarded(hub_dir, model: str | None, inner, on_refuse):
    """A runtime's turn stream with admission first. A refusal is one plain
    notice (and `on_refuse(error)` marks the run failed); nothing runs."""
    import asyncio

    from . import _request_ctx
    from .run_events import Notice

    if hub_dir is None or _CURRENT.get() is not None:
        async for item in inner():
            yield item
        return
    try:
        ticket = await asyncio.to_thread(admit, hub_dir, model=model)
    except BudgetError as exc:
        on_refuse(exc)
        _request_ctx.note_run_failure("budget")
        yield Notice(kind="error", text=exc.message, legacy=f"\n\n{exc.message}", error_kind="budget")
        return
    if ticket is None:
        async for item in inner():
            yield item
        return
    token = _CURRENT.set(ticket)
    capped = ticket.max_turns is not None or ticket.max_budget_usd is not None
    # The caller writes this turn's usage row from the request's usage; the
    # hold goes with it and is settled in the same transaction.
    handed_off = _request_ctx._current_usage.get() is not None
    try:
        async for item in inner():
            if capped and isinstance(item, Notice) and item.kind == "error" and _STOPPED.search(item.text or ""):
                # Stopped by this run's limit: say so plainly, and how to go on.
                item = Notice(kind="error", text=RUN_LIMIT, legacy=f"\n\n{RUN_LIMIT}", error_kind="run_limit")
            yield item
    finally:
        try:
            _CURRENT.reset(token)
        except ValueError:
            pass
        if handed_off and ticket.id:
            _request_ctx.note_usage(budget_reservation=ticket.id)
        elif ticket.id:
            await asyncio.to_thread(settle, hub_dir, ticket)


# -- dollar bounds where the runtime has no cost cap of its own ----------------------

def _prices(model: str | None) -> tuple[float, float] | None:
    """(input, output) USD per token from LiteLLM's pinned table, or None."""
    if not model:
        return None
    try:
        import litellm
    except ImportError:
        return None
    for name in (model, model.split("/", 1)[-1], model.rsplit("/", 1)[-1]):
        info = litellm.model_cost.get(name)
        if info and info.get("output_cost_per_token"):
            return float(info.get("input_cost_per_token") or 0), float(info["output_cost_per_token"])
    return None


def output_bound(model: str | None, prompt: str) -> int | None:
    """The most output tokens one completion may produce within this call's
    dollar allowance, or None when no dollar limit applies."""
    ticket = _CURRENT.get()
    if ticket is None or ticket.max_budget_usd is None:
        return None
    prices = _prices(model)
    if prices is None:
        return None
    left = ticket.max_budget_usd - prices[0] * (len(prompt) / 3)      # a cautious prompt estimate
    tokens = int(left / prices[1]) if prices[1] else 0
    if tokens < 1:
        raise BudgetError("budget_exhausted", "What is left of the dollar limit does not cover this request.",
                          status=429, scope="run", limit="max_cost_usd")
    return min(tokens, 1_000_000)


def cost_hooks(model):
    """Agents SDK run hooks that end the run between model calls once this
    call's dollar allowance is spent (the OpenAI Agents runtime has no cost
    cap of its own). None when no dollar limit applies."""
    ticket = _CURRENT.get()
    if ticket is None or ticket.max_budget_usd is None:
        return None
    from agents import RunHooks

    from .usage import estimate_cost

    name = getattr(model, "model", model)
    allowance = ticket.max_budget_usd

    class _Hooks(RunHooks):
        spent = 0.0

        async def on_llm_end(self, context, agent, response):
            usage = getattr(response, "usage", None)
            cost = estimate_cost(str(name) if name else None, getattr(usage, "input_tokens", 0),
                                 getattr(usage, "output_tokens", 0)) if usage else None
            self.spent += cost or 0.0
            if self.spent >= allowance:
                raise RuntimeError(f"error_max_budget_usd: this run used its {allowance:.4f} USD")

    return _Hooks()
