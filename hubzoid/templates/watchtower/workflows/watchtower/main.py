"""Watchtower: check service metrics against thresholds and explain any breach.

Every 15 minutes (and on `hubzoid schedule run <hub> watchtower`):

  1. load_events   read raw_data/events/*.jsonl (bundled synthetic data)
  2. find_breaches plain Python: which services stayed over a threshold
  3. hub.call_llm  one model call per new breach, returning a validated Finding
  4. write_report  output/watchtower/<window>.md, which the chat agent can read

Detection is deterministic code; only the explanation uses the model. A breach
already explained is remembered in `hub.state`, so later runs do not explain it
again. Thresholds live in workflows/settings.yaml.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from hubzoid import hub, step, workflow

HUB_DIR = Path(__file__).resolve().parents[2]
DEFAULTS = {"p95_ms": 800, "error_rate": 0.02, "window_minutes": 15, "min_points": 3}


class Finding(BaseModel):
    severity: str = Field(description="warning or critical")
    summary: str = Field(description="One sentence: what is wrong, with the numbers")
    likely_cause: str = Field(description="The most likely cause, citing an event if one fits")
    next_step: str = Field(description="One concrete thing an operator should check first")


@step()
def load_events() -> list[dict]:
    """Every event line under raw_data/events/. A malformed line fails the run
    with its file and line number, so the failure is easy to find and fix."""
    events = []
    for path in sorted((HUB_DIR / "raw_data" / "events").glob("*.jsonl")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name} line {n} is not valid JSON: {exc.msg}") from None
    return events


@step()
def find_breaches(events: list[dict], limits: dict) -> list[dict]:
    """Services whose last `window_minutes` of samples had at least
    `min_points` over a threshold. Deploys in or just before the window are
    attached as context."""
    samples = [e for e in events if "p95_ms" in e]
    if not samples:
        return []
    end = max(datetime.fromisoformat(e["ts"].replace("Z", "+00:00")) for e in samples)
    start = end.timestamp() - limits["window_minutes"] * 60
    breaches = []
    for service in sorted({e["service"] for e in samples}):
        window = [e for e in samples if e["service"] == service
                  and datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp() > start]
        slow = [e for e in window if e["p95_ms"] > limits["p95_ms"]]
        failing = [e for e in window if e["error_rate"] > limits["error_rate"]]
        if len(slow) < limits["min_points"] and len(failing) < limits["min_points"]:
            continue
        context = [e for e in events if e.get("service") == service and "p95_ms" not in e
                   and datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp() > start - 3600]
        breaches.append({
            "service": service,
            "window_end": end.isoformat(),
            "max_p95_ms": max(e["p95_ms"] for e in window),
            "max_error_rate": max(e["error_rate"] for e in window),
            "points_over_latency": len(slow),
            "points_over_errors": len(failing),
            "samples": len(window),
            "events": context,
        })
    return breaches


@step()
def write_report(window_end: str, results: list[dict]) -> str:
    out = HUB_DIR / "output" / "watchtower"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{window_end[:16].replace(':', '')}.md"
    lines = [f"# Watchtower report, window ending {window_end}", ""]
    for r in results:
        b, f = r["breach"], r["finding"]
        lines += [
            f"## {b['service']}: {f['severity']}",
            "",
            f["summary"],
            "",
            f"- Peak p95 latency: {b['max_p95_ms']} ms ({b['points_over_latency']} of {b['samples']} samples over)",
            f"- Peak error rate: {b['max_error_rate']:.1%} ({b['points_over_errors']} of {b['samples']} samples over)",
            f"- Likely cause: {f['likely_cause']}",
            f"- Check first: {f['next_step']}",
            "",
        ]
    lines.append("Detected by code against workflows/settings.yaml. Explained by the model.")
    path.write_text("\n".join(lines) + "\n")
    (out / "latest.md").write_text(path.read_text())
    return str(path.relative_to(HUB_DIR))


@workflow(schedule="every 15 minutes")
def watchtower():
    limits = {**DEFAULTS, **(hub.setting("watchtower") or {})}
    breaches = find_breaches(load_events(), limits)
    new = [b for b in breaches if not hub.state.get(f"explained:{b['service']}:{b['window_end']}")]
    if not new:
        return {"breaches": len(breaches), "new": 0, "report": None}
    results = []
    for b in new:
        finding = hub.call_llm(
            "A service crossed its alert thresholds. Explain it for the on-call operator.\n"
            f"Thresholds: p95 latency {limits['p95_ms']} ms, error rate {limits['error_rate']:.1%}.\n"
            f"Measurements for the last {limits['window_minutes']} minutes:\n{json.dumps(b, indent=2)}",
            system="You are a careful site reliability engineer. Use only the data given.",
            response_model=Finding,
        )
        results.append({"breach": b, "finding": finding.model_dump()})
    report = write_report(new[0]["window_end"], results)
    for b in new:
        hub.state[f"explained:{b['service']}:{b['window_end']}"] = report
    return {"breaches": len(breaches), "new": len(new), "report": report}
