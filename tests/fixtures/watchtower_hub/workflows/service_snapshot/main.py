"""A small report to personalize, run from chat and schedule for yourself."""
from __future__ import annotations

import html
import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field

from hubzoid import hub, workflow

HUB_DIR = Path(__file__).resolve().parents[2]


class SnapshotInputs(BaseModel):
    service: str | None = Field(None, description="A service from your metrics. Empty means all.")


@workflow(inputs=SnapshotInputs, as_tool=True, schedulable=True)
def service_snapshot():
    """Show latest service metrics against your saved latency rule. No model call."""
    rules = json.loads((HUB_DIR / "knowledge" / "snapshot-rules.json").read_text())
    threshold = float(rules["p95_ms"])
    latest = {}
    for path in sorted((HUB_DIR / "raw_data" / "events").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if "p95_ms" not in event:
                continue
            service = event["service"]
            if service not in latest or event["ts"] > latest[service]["ts"]:
                latest[service] = event
    wanted = hub.input.service
    selected = [e for name, e in sorted(latest.items()) if not wanted or name == wanted]
    if not selected:
        return {"refused": "No metrics match that service. Check the service name in raw_data/events."}
    rows = "".join(
        f"<tr><td>{html.escape(e['service'])}</td><td>{float(e['p95_ms']):g}</td>"
        f"<td>{'Check' if float(e['p95_ms']) > threshold else 'OK'}</td>"
        f"<td>{html.escape(e['ts'])}</td></tr>" for e in selected)
    page = hub.run_dir / "service-snapshot.html"
    page.write_text(f"<!doctype html><html lang='en'><meta charset='utf-8'>"
                   f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                   f"<title>Service snapshot</title><h1>Service snapshot</h1>"
                   f"<p>Check latency above {threshold:g} ms. Saved rule: knowledge/snapshot-rules.json.</p>"
                   f"<table><tr><th>Service</th><th>p95 ms</th><th>Status</th><th>Sample time</th></tr>{rows}</table></html>")
    # Separate stable links for all services and a filtered service; input is
    # hashed because service names need not be valid report keys.
    key = "services-all" if not wanted else "service-" + hashlib.sha256(wanted.encode()).hexdigest()[:16]
    return hub.publish_artifact(page, title="Service snapshot", key=key, audience="owner")
