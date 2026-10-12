"""People data for managers (the `gitzoid_manager` capability). Counts only,
for the digest groups an admin set on the manager as the access attribute
`gitzoid_groups`, and only when `reports.people: true`."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path

from agents import function_tool


def _common():
    path = Path(__file__).with_name("_gitzoid_common.py")
    name = "gitzoid_tools_" + hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:10]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


c = _common()
gz = c.gz


@function_tool(strict_mode=False)
@c.safe
def people_activity(person: str = "", group: str = "") -> str:
    """Who has reviews waiting, and what someone is working on, for the groups you manage: open pull requests,
    reviews requested from each person, and pull requests merged in the last 14 days. Counts only. `person` is a
    GitHub login; `group` one of your groups."""
    user, settings, store = c.ctx()
    if not settings.reports.people:
        raise PermissionError("People data is turned off here (reports.people in workflows/settings.yaml).")
    groups = gz.reports.manager_groups(getattr(user, "attrs", {}) or {}, settings)
    if not groups:
        raise PermissionError("No groups are set for you. Ask an admin to set gitzoid_groups on your access.")
    if group and group not in groups:
        raise PermissionError(f"You manage {', '.join(groups)}, not {group}.")
    repos = sorted({r for g in ([group] if group else groups) for r in settings.digest_groups[g].repos})
    gh = c.gh(settings, store)
    now = gh.now()
    open_prs = [p for r in repos for p in gh.open_pull_activity(r)]
    merged = [p for r in repos for p in gh.merged_pulls(r, now - timedelta(days=14), now)]
    rows = gz.metrics.people(merged, open_prs)
    if person:
        rows = [r for r in rows if r["person"].lower() == person.lower()]
        mine = [{"repo": p["repo"], "number": p["number"], "title": p["title"], "url": p["url"]}
                for p in open_prs if p["author"].lower() == person.lower()]
        waiting = [{"repo": p["repo"], "number": p["number"], "title": p["title"], "url": p["url"]}
                   for p in open_prs if person.lower() in [x.lower() for x in p.get("requested") or []]]
        return json.dumps({"person": person, "counts": rows, "open_pull_requests": mine,
                           "reviews_waiting_on_them": waiting}, indent=1)
    return json.dumps({"groups": [group] if group else groups, "last_14_days": rows}, indent=1)
