"""Usage rows: one per completed chat turn or workflow model call.

Hubzoid records these itself (the bridge for chat, the workflow helpers for
`call_llm` / `call_agent` / `decide`) in the operational store's `hz_usage`
table, so the Console's numbers don't depend on which chat UI fronts a hub and
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


def record(hub_dir, *, hub: str, surface: str, kind: str, subject: str | None,
           chat_id: str | None = None, model: str | None = None,
           input_tokens: int | None = None, output_tokens: int | None = None,
           cost_usd: float | None = None, status: str = "ok",
           duration_ms: int | None = None) -> None:
    """Insert one usage row. Best effort: never raises into chat or a workflow."""
    try:
        if cost_usd is None:
            cost_usd = estimate_cost(model, input_tokens, output_tokens)
        from . import db, migrations

        engine = db.operational_engine(Path(hub_dir))
        migrations.upgrade(engine, "operational")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO hz_usage (ts, hub, surface, kind, subject, chat_id, model, "
                    "input_tokens, output_tokens, cost_usd, status, duration_ms) VALUES "
                    "(:ts, :hub, :surface, :kind, :subject, :chat_id, :model, :inp, :out, "
                    ":cost, :status, :dur)"
                ),
                {
                    "ts": time.time(), "hub": hub, "surface": surface_name(surface),
                    "kind": kind, "subject": subject or None, "chat_id": chat_id,
                    "model": model, "inp": input_tokens, "out": output_tokens,
                    "cost": cost_usd, "status": status, "dur": duration_ms,
                },
            )
    except Exception:  # noqa: BLE001 — usage is telemetry; it must never break work
        log.warning("usage: could not record a %s row for %s", kind, hub, exc_info=True)


def summary(engine, hubs: dict[str, str], since: float) -> dict:
    """Usage per hub since `since` (epoch seconds), for the Console home.

    `hubs` maps the hub name stored in the rows (the folder name) to the key the
    Console routes on. Messages, conversations and active users count chat turns
    only; tokens and cost include workflow model calls. `unpriced` counts rows
    that used tokens but have no price, so a cost total is never shown as
    complete when it is not. `recording_since` is the first row ever written:
    before it, Hubzoid was not counting."""
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
            "     THEN 1 ELSE 0 END)"
            f" FROM hz_usage WHERE ts >= :s AND {in_hubs} GROUP BY hub"), params).fetchall()
        last = dict(c.execute(text(
            f"SELECT hub, MAX(ts) FROM hz_usage WHERE kind='chat' AND {in_hubs} GROUP BY hub"),
            names).fetchall())
        out["active_users"] = c.execute(text(
            f"SELECT COUNT(DISTINCT subject) FROM hz_usage WHERE kind='chat' AND ts >= :s AND {in_hubs}"),
            params).scalar() or 0
        out["recording_since"] = c.execute(text("SELECT MIN(ts) FROM hz_usage")).scalar()
    for hub, messages, chats, users, tin, tout, cost, unpriced in rows:
        key = hubs.get(hub)
        if key is None:
            continue
        out["hubs"][key] = {
            "messages": int(messages or 0), "chats": int(chats or 0), "active_users": int(users or 0),
            "input_tokens": int(tin or 0), "output_tokens": int(tout or 0),
            "cost_usd": float(cost) if cost is not None else None, "unpriced": int(unpriced or 0),
        }
    for hub, ts in last.items():
        if hub in hubs:
            out["hubs"].setdefault(hubs[hub], {})["last_activity"] = ts
    return out
