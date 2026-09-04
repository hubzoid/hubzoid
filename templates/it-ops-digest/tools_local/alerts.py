"""Tools for the it-ops-digest hub.

`alerts_since` and `open_incidents` are PLACEHOLDERS returning sample data.
Your team replaces the body of each with a call into the alerting system
(Prometheus Alertmanager, Grafana, Zabbix, Datadog) and the incident tracker
(PagerDuty, Squadcast, Jira) and keeps the signature, docstring, and return
shape.

`runbook` is real. It reads knowledge/runbooks.md in this hub and returns
the section under the heading that matches the alert name. That is the
runbook tool pattern: the runbooks stay in a markdown file the team already
edits, and the agent reaches one section at a time by name.

Files in tools_local/ are auto-discovered at boot.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents import function_tool

_HUB_DIR = Path(__file__).resolve().parent.parent
_RUNBOOKS = _HUB_DIR / "knowledge" / "runbooks.md"

_ALERTS = [
    {"time": "2026-09-01T21:14:00+05:30", "alert": "HighErrorRate", "host": "api-2", "severity": "high", "state": "fired", "incident": "INC-2210"},
    {"time": "2026-09-01T21:19:00+05:30", "alert": "HighErrorRate", "host": "api-2", "severity": "high", "state": "suppressed", "rule": "flap-damping"},
    {"time": "2026-09-01T22:02:00+05:30", "alert": "DiskFull", "host": "db-1", "severity": "high", "state": "fired", "incident": "INC-2211"},
    {"time": "2026-09-01T23:30:00+05:30", "alert": "CertExpiring", "host": "mail-1", "severity": "low", "state": "fired", "incident": ""},
    {"time": "2026-09-02T01:00:00+05:30", "alert": "BackupFailed", "host": "backup-1", "severity": "medium", "state": "fired", "incident": "INC-2212"},
    {"time": "2026-09-02T02:10:00+05:30", "alert": "HostDown", "host": "web-3", "severity": "high", "state": "suppressed", "rule": "maintenance-window"},
    {"time": "2026-09-02T02:11:00+05:30", "alert": "HighErrorRate", "host": "web-3", "severity": "high", "state": "suppressed", "rule": "dependency"},
    {"time": "2026-09-02T02:12:00+05:30", "alert": "HighErrorRate", "host": "web-3", "severity": "high", "state": "suppressed", "rule": "dependency"},
    {"time": "2026-09-02T04:45:00+05:30", "alert": "DiskFull", "host": "db-1", "severity": "high", "state": "suppressed", "rule": "flap-damping"},
]

_INCIDENTS = [
    {"id": "INC-2211", "alert": "DiskFull", "host": "db-1", "severity": "high", "owner": "night on-call", "opened": "2026-09-01T22:02:00+05:30", "hours_open": 9.0, "last_update": "2026-09-01T22:40:00+05:30", "status": "open"},
    {"id": "INC-2212", "alert": "BackupFailed", "host": "backup-1", "severity": "medium", "owner": "night on-call", "opened": "2026-09-02T01:00:00+05:30", "hours_open": 6.0, "last_update": "2026-09-02T05:30:00+05:30", "status": "open"},
    {"id": "INC-2210", "alert": "HighErrorRate", "host": "api-2", "severity": "high", "owner": "night on-call", "opened": "2026-09-01T21:14:00+05:30", "hours_open": 0.6, "last_update": "2026-09-01T21:50:00+05:30", "status": "resolved"},
]


@function_tool
def alerts_since(hours: int = 12) -> str:
    """Alerts in the last N hours, fired and suppressed. PLACEHOLDER sample data.

    Args:
        hours: Look-back window. The sample data ignores it and returns the
            whole sample night.

    Returns:
        JSON with as_of, hours, and alerts: time, alert, host, severity,
        state ("fired" or "suppressed"), incident id when one was opened,
        and rule when suppressed (maintenance-window, flap-damping, or
        dependency).
    """
    return json.dumps({"as_of": "2026-09-02T07:00:00+05:30", "hours": hours, "alerts": _ALERTS})


@function_tool
def open_incidents() -> str:
    """Incidents still open in the tracker. PLACEHOLDER sample data.

    Returns:
        JSON list of incidents with id, alert, host, severity, owner,
        opened, hours_open, last_update, and status. Only status "open" is
        returned.
    """
    return json.dumps([i for i in _INCIDENTS if i["status"] == "open"])


@function_tool
def runbook(alert_name: str) -> str:
    """Return the runbook section for one alert name from knowledge/runbooks.md.

    Args:
        alert_name: The alert name as it appears in the alerting system,
            e.g. "DiskFull". Matched case-insensitively against the
            "## Heading" lines in the runbook file.

    Returns:
        The heading and the numbered steps for that alert, or a message
        listing the runbooks that do exist.
    """
    if not _RUNBOOKS.is_file():
        return f"runbook: {_RUNBOOKS.name} not found under knowledge/. Add it with one '## AlertName' section per alert."
    wanted = alert_name.strip().lower()
    headings: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in _RUNBOOKS.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            current = title.lower()
            headings[current] = title
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    if wanted not in sections:
        known = ", ".join(headings[k] for k in sorted(headings))
        return f"runbook: no section for '{alert_name}'. Known runbooks: {known}. Add a '## {alert_name}' section to knowledge/runbooks.md."
    body = "\n".join(sections[wanted]).strip()
    return f"## {headings[wanted]}\n{body}"
