"""Per-interaction token/cost ledger — append-only JSONL, one line per turn.

Nothing tracked cost before: the bridge zeroed the OpenAI usage envelope and
the agent runs OUTSIDE Open WebUI (a subprocess), so OWUI's own accounting saw
nothing. But the backends already report usage — the Claude SDK's
`ResultMessage` carries `usage` + `total_cost_usd`, the OpenAI Agents run
carries `context_wrapper.usage`. The runtimes now surface it via
`_request_ctx.record_usage`; the bridge drains it per turn and appends here.

Storage is a hub-local JSONL file at
`<hub>/.hubzoid/metrics/interactions.jsonl` — append-only, so a restart or a
crash mid-write loses at most the last partial line and never the file. NOT
Open WebUI's database: that schema is OWUI's to migrate and coupling to it
fights the white-label/upgrade story. `summary()` aggregates the ledger for the
bridge's `/metrics` endpoint (and, via a dashboard artifact, for a UI).

Cost note: on the `claude-local` subscription there is no per-call dollar cost,
so `cost_usd` is None there — token counts still give relative cost per
hub/user/model. `total_cost_usd` is only meaningful in API-key mode.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("hubzoid.metrics")

STATE_DIRNAME = ".hubzoid"


def ledger_path(hub_dir: Path) -> Path:
    return Path(hub_dir) / STATE_DIRNAME / "metrics" / "interactions.jsonl"


def _as_int(v) -> int:
    """Coerce to int, never raising (a bad value becomes 0)."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def record_interaction(hub_dir: Path, *, chat_id: str | None, identity,
                       model: str, usage: dict) -> None:
    """Append one interaction to the ledger. Never raises.

    Skips writing when nothing was measured (input+output tokens both zero) —
    that means the backend reported no usage (e.g. an older SDK), and a
    zero-token line is just noise.
    """
    inp = _as_int(usage.get("input_tokens"))
    out = _as_int(usage.get("output_tokens"))
    if inp <= 0 and out <= 0:
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chat_id": chat_id,
        "user": getattr(identity, "user", None),
        "surface": getattr(identity, "surface", None),
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "cost_usd": usage.get("cost_usd"),
        "num_turns": usage.get("num_turns"),
    }
    try:
        path = ledger_path(hub_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        log.warning("metrics: could not append ledger: %s", exc)


def iter_entries(hub_dir: Path):
    """Yield ledger entries one at a time. A corrupt/half-written line is
    skipped, not fatal. Streaming so aggregation never holds the whole ledger
    in memory — a hub with millions of turns aggregates in O(1) extra space."""
    path = ledger_path(hub_dir)
    if not path.is_file():
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as exc:
        log.warning("metrics: could not read ledger: %s", exc)


def read_entries(hub_dir: Path) -> list[dict]:
    """Every ledger entry as a list (convenience for tests/small consumers).
    Prefer `iter_entries` on the hot path."""
    return list(iter_entries(hub_dir))


def _blank() -> dict:
    return {"interactions": 0, "input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0}


def _add(bucket: dict, e: dict) -> None:
    bucket["interactions"] += 1
    # _as_int (never raises) so a hand-tampered but valid-JSON ledger line with
    # a non-numeric token field can't 500 the /metrics endpoint.
    bucket["input_tokens"] += _as_int(e.get("input_tokens"))
    bucket["output_tokens"] += _as_int(e.get("output_tokens"))
    bucket["total_tokens"] += _as_int(e.get("total_tokens"))
    cost = e.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        bucket["cost_usd"] += cost


def summary(hub_dir: Path) -> dict:
    """Aggregate the ledger by model / user / day, plus grand totals.

    The shape a cost dashboard (feature #6) or an external analytics job can
    consume directly. Streams the ledger (via `iter_entries`) so only the
    buckets — O(distinct model/user/day) — stay resident, never the whole file.
    `cost_usd` totals are 0.0 on subscription backends where no dollar cost is
    reported.
    """
    count = 0
    total = _blank()
    by_model: dict[str, dict] = {}
    by_user: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    for e in iter_entries(hub_dir):
        count += 1
        _add(total, e)
        _add(by_model.setdefault(e.get("model") or "unknown", _blank()), e)
        _add(by_user.setdefault(e.get("user") or "anonymous", _blank()), e)
        _add(by_day.setdefault((e.get("ts") or "")[:10] or "unknown", _blank()), e)
    return {
        "count": count,
        "totals": total,
        "by_model": by_model,
        "by_user": by_user,
        "by_day": by_day,
    }
