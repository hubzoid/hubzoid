"""Usage rows: one per completed chat turn or workflow model call.

Hubzoid records these itself (the bridge for chat, the workflow helpers for
`call_llm` / `call_agent`, and Jev for `call_jev` in workflows and chat) in the
operational store's `hz_usage` table, so the Console's numbers don't depend on which chat UI fronts a hub and
include Slack, WhatsApp and Telegram. No message content is stored.

Cost is an estimate: the backend's own figure when it reports one, otherwise
LiteLLM's price table for the model, otherwise unknown (NULL).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import text

log = logging.getLogger("hubzoid.usage")

# Identity surfaces -> the Console's channel names.
_SURFACES = {"owui": "web", "web": "web", "api": "api", "system": "api", "slack": "slack",
             "slack-channel": "slack", "slack-dm": "slack", "whatsapp": "whatsapp",
             "telegram": "telegram", "workflow": "workflow", "mcp": "mcp"}


def surface_name(raw: str | None) -> str:
    return _SURFACES.get((raw or "").strip().lower(), (raw or "api").strip().lower() or "api")


def estimate_cost(model: str | None, input_tokens: int | None,
                  output_tokens: int | None) -> float | None:
    """LiteLLM's price-table estimate for `model`, or None when it's unknown."""
    if not model or (not input_tokens and not output_tokens):
        return None
    try:
        import litellm
    except ImportError:
        return None
    # An unknown model (codex-local's, say) makes LiteLLM print its provider
    # list to the terminal; a missing price is not worth a message.
    litellm.suppress_debug_info = True
    candidates = [model]
    if "/" in model:
        candidates.append(model.split("/", 1)[1])  # openrouter/anthropic/x -> anthropic/x
        candidates.append(model.rsplit("/", 1)[1])
    for name in candidates:
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=name,
                prompt_tokens=int(input_tokens or 0),
                completion_tokens=int(output_tokens or 0),
            )
            return float(prompt_cost + completion_cost)
        except Exception:  # noqa: BLE001 — unknown model: try the next spelling
            continue
    return None


from contextlib import contextmanager
from contextvars import ContextVar

#: The workflow run a model call belongs to, so its cost can be summed per run
#: and per personal schedule. Set by the workflow run context.
_RUN_TAG: ContextVar["str | None"] = ContextVar("hubzoid_usage_run", default=None)
#: What started that run (see TRIGGERS), so its rows say how agents were used.
_TRIGGER: ContextVar["str | None"] = ContextVar("hubzoid_usage_trigger", default=None)

#: Values of hz_usage.origin (what started a call). NULL: the row predates it.
TRIGGERS = ("chat", "schedule", "manual", "event", "mcp", "eval")

# A workflow run's own trigger (WorkflowContext.trigger) -> hz_usage.origin.
_RUN_TRIGGERS = {"schedule": "schedule", "personal_schedule": "schedule", "operator": "manual",
                 "cli": "manual", "webhook": "event", "update": "event", "work_item": "manual",
                 "api": "manual"}  # a "Run for me" report started over the API (POST /portal/api/my/runs)
# Surfaces a person talks to an agent on (the run's envelope `via`).
_CONVERSATION = {"web", "api", "slack", "whatsapp", "telegram"}


def run_trigger(trigger: str | None, via: str | None = None) -> str | None:
    """hz_usage.origin for a workflow run started by `trigger`. A run that a
    tool or a conversation started (`via`, the surface it was asked on)
    belongs to that conversation: mcp over MCP, chat otherwise. A start from
    the Console or the CLI is manual."""
    where = surface_name(via) if via else None
    if trigger == "tool":
        return "mcp" if where == "mcp" else "chat"
    if trigger == "operator" and where == "mcp":
        return "mcp"
    if trigger == "operator" and where in _CONVERSATION:
        return "chat"
    return _RUN_TRIGGERS.get(trigger or "")


def markdown_origin(run_id: str | None, events: list | None = None) -> str:
    """hz_usage.origin of a markdown task run, from its DBOS id's slot:
    `manual-...` is a start by hand, `events-...` or claimed webhook events an
    event, anything else a schedule slot."""
    from .workflows.markdown import slot_from_id

    slot = slot_from_id(run_id or "") or ""
    if slot.startswith("manual-"):
        return "manual"
    if slot.startswith("events-") or events:
        return "event"
    return "schedule"


def litellm_details(u) -> dict:
    """Cache and reasoning tokens from an OpenAI-style usage object or dict
    (LiteLLM, the OpenAI Agents SDK): `prompt_tokens_details.cached_tokens` or
    `input_tokens_details.cached_tokens`, and the matching reasoning field.
    Both are parts of the input and output totals, as OpenAI reports them."""
    def get(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    out = {}
    try:
        inp = get(u, "prompt_tokens_details") or get(u, "input_tokens_details")
        outp = get(u, "completion_tokens_details") or get(u, "output_tokens_details")
        cached = get(inp, "cached_tokens") if inp is not None else None
        reasoning = get(outp, "reasoning_tokens") if outp is not None else None
        if cached is not None:
            out["cache_read_tokens"] = int(cached)
        if reasoning is not None:
            out["reasoning_tokens"] = int(reasoning)
    except (TypeError, ValueError):
        return {}
    return out


@contextmanager
def run_tag(run_id: str | None, trigger: str | None = None):
    token = _RUN_TAG.set(run_id or None)
    trig = _TRIGGER.set(trigger) if trigger else None
    try:
        yield
    finally:
        if trig is not None:
            _TRIGGER.reset(trig)
        _RUN_TAG.reset(token)


#: Optional usage details a backend may report, carried from its usage dict to
#: the row as they are: cache and reasoning tokens (parts of input_tokens and
#: output_tokens) and, for a chat turn, the time to the first answer text.
DETAILS = ("cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "first_token_ms")


def usage_details(raw: dict | None) -> dict:
    """The DETAILS present in a backend's usage dict, as non-negative ints."""
    out = {}
    for key in DETAILS:
        try:
            value = (raw or {}).get(key)
            if value is not None and int(value) >= 0:
                out[key] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def run_cost(hub_dir, hub: str, prefix: str, since: float) -> float:
    """Model cost of runs whose id starts with `prefix`, since `since`."""
    from sqlalchemy import text as _text

    from . import db, migrations

    engine = db.operational_engine(Path(hub_dir))
    migrations.upgrade(engine, "operational")
    with engine.connect() as conn:
        value = conn.execute(_text("SELECT SUM(cost_usd) FROM hz_usage WHERE hub=:h AND chat_id LIKE :p "
                                   "AND ts >= :t"), {"h": hub, "p": prefix + "%", "t": since}).scalar()
    return float(value or 0.0)


def _cost_from_cli(model: str | None) -> bool:
    """A Claude call on Bedrock: the Claude CLI's own figure is the cost, and a
    missing one stays unknown instead of a price-table estimate."""
    from . import bedrock

    return bedrock.is_claude(model, resolve=False)


def record(hub_dir, *, hub: str, surface: str, kind: str, subject: str | None,
           chat_id: str | None = None, model: str | None = None,
           input_tokens: int | None = None, output_tokens: int | None = None,
           cost_usd: float | None = None, status: str = "ok",
           duration_ms: int | None = None, reservation: str | None = None,
           trigger: str | None = None, cache_read_tokens: int | None = None,
           cache_write_tokens: int | None = None, reasoning_tokens: int | None = None,
           first_token_ms: int | None = None) -> None:
    """Insert one usage row. Best effort: never raises into chat or a workflow.
    `reservation` (budgets.yaml) is settled in the same transaction; if the row
    cannot be written, the hold stays as unknown usage.

    `trigger` defaults to the enclosing run's (run_tag), else mcp for the mcp
    surface and chat for the other conversation surfaces. An untagged workflow
    row stays NULL rather than guessed."""
    chat_id = chat_id or _RUN_TAG.get()
    try:
        surface = surface_name(surface)
        if trigger not in TRIGGERS:
            trigger = _TRIGGER.get() or ("mcp" if surface == "mcp" else
                                         None if surface == "workflow" else "chat")
        if cost_usd is None and not _cost_from_cli(model):
            cost_usd = estimate_cost(model, input_tokens, output_tokens)
        from . import db, migrations

        engine = db.operational_engine(Path(hub_dir))
        migrations.upgrade(engine, "operational")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO hz_usage (ts, hub, surface, kind, subject, chat_id, model, "
                    "input_tokens, output_tokens, cost_usd, status, duration_ms, origin, "
                    "cache_read_tokens, cache_write_tokens, reasoning_tokens, first_token_ms) VALUES "
                    "(:ts, :hub, :surface, :kind, :subject, :chat_id, :model, :inp, :out, "
                    ":cost, :status, :dur, :trigger, :cr, :cw, :rs, :ft)"
                ),
                {
                    "ts": time.time(), "hub": hub, "surface": surface,
                    "kind": kind, "subject": subject or None, "chat_id": chat_id,
                    "model": model, "inp": input_tokens, "out": output_tokens,
                    "cost": cost_usd, "status": status, "dur": duration_ms, "trigger": trigger,
                    "cr": cache_read_tokens, "cw": cache_write_tokens, "rs": reasoning_tokens,
                    "ft": first_token_ms,
                },
            )
            if reservation:
                from .budgets import settle_in
                settle_in(conn, reservation)
    except Exception:  # noqa: BLE001 — usage is telemetry; it must never break work
        log.warning("usage: could not record a %s row for %s", kind, hub, exc_info=True)


def summary(engine, hubs: dict[str, str], since: float) -> dict:
    """Usage per hub since `since` (epoch seconds), for the Console home.

    `hubs` maps the hub name stored in the rows (the folder name) to the key the
    Console routes on. Messages, conversations and active users count chat turns
    only; tokens and cost include workflow model calls. `unpriced` counts rows
    that used tokens but have no price, so a cost total is never shown as
    complete when it is not. `recording_since` is the first row written for
    these hubs: numbers before it are not known to be complete. Rows of other
    hubs never inform it, so a hub admin learns nothing of hubs outside their
    scope."""
    out: dict = {"hubs": {}, "active_users": 0, "recording_since": None}
    if not hubs:
        return out
    from . import migrations

    migrations.upgrade(engine, "operational")
    names = {f"h{i}": name for i, name in enumerate(hubs)}
    in_hubs = "hub IN (" + ", ".join(f":{k}" for k in names) + ")"
    params = {"s": since, **names}
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT hub,"
            " SUM(CASE WHEN kind='chat' THEN 1 ELSE 0 END),"
            " COUNT(DISTINCT CASE WHEN kind='chat' THEN chat_id END),"
            " COUNT(DISTINCT CASE WHEN kind='chat' THEN subject END),"
            " SUM(input_tokens), SUM(output_tokens), SUM(cost_usd),"
            " SUM(CASE WHEN cost_usd IS NULL AND COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) > 0"
            "     THEN 1 ELSE 0 END), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)"
            f" FROM hz_usage WHERE ts >= :s AND {in_hubs} GROUP BY hub"), params).fetchall()
        last = dict(c.execute(text(
            f"SELECT hub, MAX(ts) FROM hz_usage WHERE kind='chat' AND {in_hubs} GROUP BY hub"),
            names).fetchall())
        out["active_users"] = c.execute(text(
            f"SELECT COUNT(DISTINCT subject) FROM hz_usage WHERE kind='chat' AND ts >= :s AND {in_hubs}"),
            params).scalar() or 0
        out["recording_since"] = c.execute(text(
            f"SELECT MIN(ts) FROM hz_usage WHERE {in_hubs}"), names).scalar()
    for hub, messages, chats, users, tin, tout, cost, unpriced, errors in rows:
        key = hubs.get(hub)
        if key is None:
            continue
        out["hubs"][key] = {
            "messages": int(messages or 0), "chats": int(chats or 0), "active_users": int(users or 0),
            "input_tokens": int(tin or 0), "output_tokens": int(tout or 0),
            "cost_usd": float(cost) if cost is not None else None, "unpriced": int(unpriced or 0),
            "errors": int(errors or 0),
        }
    for hub, ts in last.items():
        if hub in hubs:
            out["hubs"].setdefault(hubs[hub], {})["last_activity"] = ts
    return out


# -- Usage page -------------------------------------------------------------------

def _percentile(values: list, pct: float) -> int | None:
    """Nearest-rank percentile of `values` (already sorted), or None if empty."""
    if not values:
        return None
    rank = max(1, -(-len(values) * pct // 100))  # ceil(n * pct / 100)
    return int(values[min(len(values), int(rank)) - 1])


def _bucket_expr(engine) -> str:
    """Index of the time bucket a row's ts falls in, counted from :s. SQLite's
    CAST truncates, which is floor for the non-negative offsets used here;
    Postgres' CAST rounds, so it uses FLOOR."""
    if engine.dialect.name == "postgresql":
        return "CAST(FLOOR((ts - :s) / :b) AS INTEGER)"
    return "CAST((ts - :s) / :b AS INTEGER)"


_TOTALS = (
    "COUNT(*), SUM(CASE WHEN kind='chat' THEN 1 ELSE 0 END),"
    " COUNT(DISTINCT CASE WHEN kind='chat' THEN chat_id END),"
    " COUNT(DISTINCT CASE WHEN kind='chat' THEN subject END),"
    " SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), SUM(cache_write_tokens),"
    " SUM(reasoning_tokens), SUM(cost_usd),"
    " SUM(CASE WHEN cost_usd IS NULL AND COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) > 0"
    "     THEN 1 ELSE 0 END),"
    " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END)")
_TOTAL_KEYS = ("calls", "messages", "chats", "people", "input_tokens", "output_tokens", "cache_read_tokens",
               "cache_write_tokens", "reasoning_tokens", "cost_usd", "unpriced", "errors", "cancelled")


def _totals(row) -> dict:
    out = {}
    for key, value in zip(_TOTAL_KEYS, row):
        if key == "cost_usd":
            out[key] = round(float(value), 6) if value is not None else None
        elif key == "reasoning_tokens":
            out[key] = int(value) if value is not None else None
        else:
            out[key] = int(value or 0)
    return out


def _hub_filter(hubs: dict[str, str]) -> tuple[str, dict]:
    names = {f"h{i}": name for i, name in enumerate(hubs)}
    return "hub IN (" + ", ".join(f":{k}" for k in names) + ")", names


def insights(engine, hubs: dict[str, str], since: float, until: float, bucket: int) -> dict:
    """Everything the Usage page shows for one period, from hz_usage alone.

    `hubs` maps the hub name stored in the rows to the key the Console routes
    on, as for summary(). Rows of other hubs never inform any number. Buckets
    are `bucket` seconds wide, counted from `since`. `previous` holds the same
    totals for the window before `since`, or None when recording started after
    that window began, so a change is never shown against missing data.
    Response times leave out failed calls and background title calls."""
    n = max(1, int(-(-(until - since) // bucket)))
    out: dict = {"bucket": bucket, "since": since, "until": until, "recording_since": None,
                 "totals": _totals([0] * len(_TOTAL_KEYS)), "previous": None,
                 "series": [{"start": since + i * bucket, "input_tokens": 0, "cache_read_tokens": 0,
                             "output_tokens": 0, "cost_usd": 0.0, "p50_ms": None, "p95_ms": None}
                            for i in range(n)],
                 "by_surface": [], "by_trigger": [], "by_model": [], "by_hub": [],
                 "latency": {"first_token_p50_ms": None, "p50_ms": None, "p95_ms": None}}
    if not hubs:
        return out
    from . import migrations

    migrations.upgrade(engine, "operational")
    in_hubs, names = _hub_filter(hubs)
    p = {"s": since, "u": until, "b": bucket, **names}
    where = f"ts >= :s AND ts < :u AND {in_hubs}"
    with engine.connect() as c:
        out["recording_since"] = c.execute(text(f"SELECT MIN(ts) FROM hz_usage WHERE {in_hubs}"),
                                           names).scalar()
        out["totals"] = _totals(c.execute(text(f"SELECT {_TOTALS} FROM hz_usage WHERE {where}"), p).one())
        prev_since = since - (until - since)
        rec = out["recording_since"]
        if rec is not None and rec <= prev_since:
            out["previous"] = _totals(c.execute(text(
                f"SELECT {_TOTALS} FROM hz_usage WHERE ts >= :ps AND ts < :s AND {in_hubs}"),
                {**p, "ps": prev_since}).one())
        for idx, inp, cache, outp, cost in c.execute(text(
                f"SELECT {_bucket_expr(engine)} AS i, SUM(input_tokens), SUM(cache_read_tokens),"
                f" SUM(output_tokens), SUM(cost_usd) FROM hz_usage WHERE {where} GROUP BY i"), p):
            if idx is not None and 0 <= int(idx) < n:
                out["series"][int(idx)].update(input_tokens=int(inp or 0), cache_read_tokens=int(cache or 0),
                                               output_tokens=int(outp or 0), cost_usd=float(cost or 0))
        for surface, calls, msgs, tokens, cost in c.execute(text(
                "SELECT surface, COUNT(*), SUM(CASE WHEN kind='chat' THEN 1 ELSE 0 END),"
                " SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), SUM(cost_usd)"
                f" FROM hz_usage WHERE {where} GROUP BY surface"), p):
            out["by_surface"].append({"surface": surface, "calls": int(calls), "messages": int(msgs or 0),
                                      "tokens": int(tokens or 0), "cost_usd": float(cost or 0)})
        for trig, calls, cost in c.execute(text(
                f"SELECT origin, COUNT(*), SUM(cost_usd) FROM hz_usage WHERE {where} GROUP BY origin"), p):
            out["by_trigger"].append({"trigger": trig, "calls": int(calls), "cost_usd": float(cost or 0)})
        for row in c.execute(text(
                "SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),"
                " SUM(cache_write_tokens), SUM(reasoning_tokens), SUM(cost_usd),"
                " SUM(CASE WHEN cost_usd IS NULL AND COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0) > 0"
                "     THEN 1 ELSE 0 END), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)"
                f" FROM hz_usage WHERE {where} GROUP BY model"), p):
            model, calls, inp, outp, cr, cw, rs, cost, unpriced, errors = row
            out["by_model"].append({"model": model, "calls": int(calls), "input_tokens": int(inp or 0),
                                    "output_tokens": int(outp or 0), "cache_read_tokens": int(cr or 0),
                                    "cache_write_tokens": int(cw or 0),
                                    "reasoning_tokens": int(rs) if rs is not None else None,
                                    "cost_usd": float(cost) if cost is not None else None,
                                    "unpriced": int(unpriced or 0), "errors": int(errors or 0)})
        by_hub = {}
        for row in c.execute(text(f"SELECT hub, {_TOTALS} FROM hz_usage WHERE {where} GROUP BY hub"), p):
            key = hubs.get(row[0])
            if key is not None:
                by_hub[key] = {"key": key, **_totals(row[1:]), "p50_ms": None, "p95_ms": None}
        durations: dict = {}
        firsts: list = []
        for hub, ts, dur, first in c.execute(text(
                "SELECT hub, ts, duration_ms, first_token_ms FROM hz_usage"
                f" WHERE {where} AND status='ok' AND kind != 'background'"
                " AND (duration_ms IS NOT NULL OR first_token_ms IS NOT NULL)"), p):
            if dur is not None:
                durations.setdefault(hub, []).append((ts, int(dur)))
            if first is not None:
                firsts.append(int(first))
    every = sorted(d for rows in durations.values() for _, d in rows)
    out["latency"] = {"first_token_p50_ms": _percentile(sorted(firsts), 50),
                      "p50_ms": _percentile(every, 50), "p95_ms": _percentile(every, 95)}
    per_bucket: dict = {}
    for hub, rows in durations.items():
        values = sorted(d for _, d in rows)
        key = hubs.get(hub)
        if key in by_hub:
            by_hub[key].update(p50_ms=_percentile(values, 50), p95_ms=_percentile(values, 95))
        for ts, d in rows:
            per_bucket.setdefault(int((ts - since) // bucket), []).append(d)
    for idx, values in per_bucket.items():
        if 0 <= idx < n:
            values.sort()
            out["series"][idx].update(p50_ms=_percentile(values, 50), p95_ms=_percentile(values, 95))
    out["by_hub"] = sorted(by_hub.values(), key=lambda r: -(r["cost_usd"] or 0))
    for key in ("by_surface", "by_trigger", "by_model"):
        out[key].sort(key=lambda r: (-(r["cost_usd"] or 0), -r["calls"]))
    return out


def subject_kind(subject: str | None) -> str:
    """person, schedule (a legacy markdown task), workflow, or system for a row
    with no subject (evals, and calls made outside a person's request)."""
    if not subject:
        return "system"
    s = subject
    if s.startswith("workflow:md:"):
        return "schedule"
    if s.startswith("workflow:"):
        return "workflow"
    return "person"


_PEOPLE_SORTS = {"cost": "cost_usd", "tokens": "tokens", "messages": "messages", "name": "subject",
                 "last": "last"}


def people(engine, hubs: dict[str, str], since: float, until: float, *, q: str = "", sort: str = "cost",
           descending: bool = True, offset: int = 0, limit: int = 8) -> dict:
    """Who used the agents in a period: people and service subjects, with the
    agents they used, messages, tokens, cost and last activity. Searched by
    subject or agent name, sorted, then paged. Rows of other hubs are never
    read, so a delegate learns nothing of hubs outside their scope."""
    out = {"rows": [], "total": 0}
    if not hubs:
        return out
    from . import migrations

    migrations.upgrade(engine, "operational")
    in_hubs, names = _hub_filter(hubs)
    rows: dict = {}
    with engine.connect() as c:
        for subject, hub, msgs, tokens, cost, last in c.execute(text(
                "SELECT subject, hub, SUM(CASE WHEN kind='chat' THEN 1 ELSE 0 END),"
                " SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), SUM(cost_usd), MAX(ts)"
                f" FROM hz_usage WHERE ts >= :s AND ts < :u AND {in_hubs} GROUP BY subject, hub"),
                {"s": since, "u": until, **names}):
            key = hubs.get(hub)
            if key is None:
                continue
            row = rows.setdefault(subject, {"subject": subject, "kind": subject_kind(subject), "agents": [],
                                            "messages": 0, "tokens": 0, "cost_usd": 0.0, "last": 0.0})
            row["agents"].append(key)
            row["messages"] += int(msgs or 0)
            row["tokens"] += int(tokens or 0)
            row["cost_usd"] += float(cost or 0)
            row["last"] = max(row["last"], float(last or 0))
    needle = (q or "").strip().lower()
    found = [r for r in rows.values()
             if not needle or needle in (r["subject"] or "evals and system calls").lower()
             or any(needle in a.lower() for a in r["agents"])]
    field = _PEOPLE_SORTS.get(sort, "cost_usd")
    found.sort(key=lambda r: (r[field] if field != "subject" else (r["subject"] or "").lower()),
               reverse=descending)
    for r in found:
        r["agents"].sort()
        r["cost_usd"] = round(r["cost_usd"], 6)
    out["total"] = len(found)
    out["rows"] = found[max(0, offset):max(0, offset) + max(1, min(limit, 100))]
    return out
