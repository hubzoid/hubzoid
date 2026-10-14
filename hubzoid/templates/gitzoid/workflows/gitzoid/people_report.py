"""people_report: counts per person for a manager's own groups. Pull requests
merged, reviews given, reviews waiting on them, open now. No scores and no
rankings, and never a team page or a list email.

Three things must all hold:
  1. `reports.people: true` in workflows/settings.yaml;
  2. the person holds this workflow's "Run for me" grant (checked by Hubzoid
     on every run, including their own schedules);
  3. an admin set `gitzoid_groups` on their access (the digest groups they
     manage); only those groups' repos are counted.
The page is private to the manager who ran it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

from hubzoid import hub, workflow


def _load(file: str):
    path = Path(__file__).with_name(file)
    name = f"gitzoid_{path.stem.strip('_')}_" + hashlib.sha1(str(path).encode()).hexdigest()[:10]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


reports = _load("_reports.py")
gz = reports.gz


def refuse(message: str) -> dict:
    """A tool caller sees this sentence (Hubzoid shows only fixed causes for
    failed tool runs); a scheduled run fails, so a broken schedule pauses."""
    if hub.trigger == "tool":
        return {"refused": message}
    raise PermissionError(message)


def allowed_scope(req, settings, groups: list[str]) -> tuple[str, list[str]]:
    mine = {r for g in groups for r in settings.digest_groups[g].repos}
    if req.group:
        if req.group not in groups:
            raise PermissionError(f"You manage {', '.join(groups)}, not {req.group}.")
        return req.group, list(settings.digest_groups[req.group].repos)
    if req.repos:
        outside = [r for r in req.repos if r not in mine]
        if outside:
            raise PermissionError(f"Not in the groups you manage: {', '.join(outside)}.")
        return ", ".join(req.repos), list(req.repos)
    return ", ".join(groups), sorted(mine)


@workflow(inputs=reports.gz.reports.ReportRequest, as_tool=True, schedulable=True, catch_up=True,
          sensitive=True, surfaces=("web", "owui", "mcp"))
def people_report():
    """People report for a manager's own digest groups: merged work, reviews waiting and open pull requests per person."""
    req = hub.input
    settings = gz.settings.load(hub.setting("gitzoid"))
    if not settings.reports.people:
        return refuse("People reports are turned off here (reports.people in workflows/settings.yaml).")
    if hub.team_run:
        return refuse("The people report runs only for the manager who asks, never as a team report.")
    if hub.trigger == "personal_schedule" and not req.rolling:
        return refuse("A schedule needs a rolling period, such as the last 14 days; fixed dates never change.")
    groups = gz.reports.manager_groups(hub.user.attrs, settings)
    if not groups:
        return refuse("No groups are set for you. Ask an admin to set gitzoid_groups on your access.")
    try:
        name, repos = allowed_scope(req, settings, groups)
    except PermissionError as exc:
        return refuse(str(exc))
    start, end, label = gz.reports.period(req, gz.github.ts(reports.clock()))
    data = reports.gather(repos, start.isoformat(), end.isoformat(), prev=False)
    rows = gz.metrics.people(data["merged"], data["open"])
    html = gz.render.people_page(name, label, rows)
    key = f"gitzoid-people-{gz.reports.slug(name)}"
    art = gz.runtime.publish(hub, key, html, f"People report · {name}", key, digest=gz.runtime.digest_of(rows, label))
    if hub.trigger == "personal_schedule":
        totals = {"people": len(rows), **{k: sum(r[k] for r in rows) for k in ("merged", "reviews_given", "open")}}
        subject, body = gz.mail.report_email("people", name, label, {
            "headline": f"Your private report covers {len(rows)} people in the groups you manage.",
            "highlights": ["Activity counts only; no scores or rankings. Individual details stay in the report."]},
            totals, link=gz.runtime.link_of(art))
        hub.send_email(subject[:200], body, artifacts=[art], raise_on_failure=False, body_format="html")
    return {"report": name, "period": label, "people": len(rows), "link": gz.runtime.link_of(art)}
