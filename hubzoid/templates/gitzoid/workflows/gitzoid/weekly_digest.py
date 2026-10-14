"""weekly_digest: every Monday, one digest per digest group.

What shipped (read from the descriptions and diffs), team numbers with the
change from the week before, security status, GitZoid's report card and the
health of the team's rules. Published as the group's live team page (one URL
that always shows the newest copy, open to anyone signed in with the company
domain) and emailed to the group's list as a short summary and a link. A
quiet week still gets a short digest, with no model call.

With no digest groups set, one digest covers every repo and goes to
owner_list.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

from hubzoid import hub, step, workflow


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
flow = reports.flow
gz = reports.gz


@step()
def extras(repos: list[str], since_30d: str, start: str) -> dict:
    store = gz.store.Store(hub.data_dir)
    rules = gz.rules.hub_rules(flow.HUB_DIR)
    return {"security": {"open": store.ledger_rows(repos, state="open"), "coverage": store.coverage(repos),
                         "audit_in_progress": [r for r in repos if store.audit(r)["started_at"]]},
            "health": gz.digest.rule_health(rules, store.finding_events(since_30d, repos))}


@workflow(schedule="every monday at 08:00", catch_up=True)
def weekly_digest():
    settings = gz.settings.load(hub.setting("gitzoid"))
    groups = {g: (grp.repos, grp.email_list) for g, grp in settings.digest_groups.items()} or \
        {"all": (settings.repo_names(), settings.owner_list)}
    now = gz.github.ts(reports.clock())
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=settings.digest.days)
    label = f"{start:%d %b} to {(end - timedelta(days=1)):%d %b %Y}"
    week_of = f"{(end - timedelta(days=1)):%d %b %Y}"
    out = {}
    for group, (repos, email_list) in groups.items():
        data = reports.enrich(reports.gather(repos, start.isoformat(), end.isoformat(), prev=True, patches=True))
        x = extras(repos, (end - timedelta(days=30)).isoformat(), start.isoformat())
        now_d = gz.metrics.delivery(data["merged"], data["open"], now)
        prev_d = gz.metrics.delivery(data["prev"], [], now)
        now_c = gz.metrics.code(data["merged"], data["events"])
        card = gz.digest.report_card(data["events"], data["reviews"])
        numbers = {"merged": now_d["merged"], "merged_week_before": prev_d["merged"],
                   "cycle_hours_median": now_d["cycle_hours_median"],
                   "first_review_hours_median": now_d["first_review_hours_median"],
                   "open": now_d["open"], "stuck": now_d["stuck"], "additions": now_c["additions"],
                   "deletions": now_c["deletions"], "security_open": len(x["security"]["open"]),
                   "contributors": len({p.get("author") for p in data["merged"] if p.get("author")})}
        problem = None if not data["merged"] else flow.charge(1)
        d = None
        if data["merged"] and not problem:
            try:
                d = flow.ask(gz.digest.prompt(group, label, data["merged"], numbers, settings.digest.poem),
                             system=gz.digest.SYSTEM, response_model=gz.models.Digest,
                             model=settings.digest.model).model_dump()
            except flow.Unreadable as exc:
                problem = f"The summary could not be written this week ({exc}); the numbers below are complete."
        if d is None:
            d = {"headline": f"A quiet week in {group}: nothing was merged." if not data["merged"]
                 else f"{len(data['merged'])} pull requests merged in {group}.",
                 "for_leadership": [f"{now_d['open']} pull requests are open, {now_d['stuck']} without an update "
                                    f"for {gz.metrics.STUCK_DAYS}+ days."] + ([problem] if problem else []),
                 "technical": [], "areas": [], "watch": [], "poem": ""}
        html = gz.render.digest_page(group, label, d, now_d, prev_d, now_c, data["merged"], x["security"], card,
                                     x["health"], data["coverage"])
        art = gz.runtime.publish(hub, f"digest-{group}", html, f"Weekly digest · {group}", f"gitzoid-digest-{gz.reports.slug(group)}",
                                 digest=gz.runtime.digest_of(d, numbers, label))
        subject, body = gz.mail.digest_email(group, label, week_of, d, numbers, x["security"], data["coverage"],
                                             gz.runtime.link_of(art), gz.runtime.pr_repos(settings),
                                             implicit=not settings.digest_groups)
        sent = None if gz.runtime.refresh_only(hub) else gz.runtime.email_list(hub, email_list, subject, body, art)
        out[group] = {"headline": d["headline"], "merged": now_d["merged"], "page": gz.runtime.link_of(art),
                      "shared_with": art.get("audience"), "emailed": email_list if sent and gz.runtime.delivered(sent) else None, "email_delivery": sent}
    return out
