"""security_watch: every day, every repo's dependencies against OSV.dev and the
CISA Known Exploited Vulnerabilities catalog.

  1. checkout     the default branch, read-only, in the private data folder
  2. parse        lockfiles for Python, npm, Go, Rust, Ruby and PHP
  3. feeds        OSV advisories and the KEV catalog (code decides what counts)
  4. reachability where the code imports each package
  5. hub.call_llm judge reachability and write impact and fix, once per advisory
  6. ledger       new, worse and resolved, so nothing is alerted twice
  7. per group    a page for the agent's users and an email to the group's list

A check that could not run (a feed outage, an unreadable lockfile, a repo the
token cannot read) is reported in the email as a coverage problem, never as
silence. Pull requests that change a lockfile are checked in their review.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

from hubzoid import hub, step, workflow


def _flow():
    path = Path(__file__).with_name("_flow.py")
    name = "gitzoid_flow_" + hashlib.sha1(str(path).encode()).hexdigest()[:10]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


flow = _flow()
gz = flow.gz


@step()
def security_plan() -> dict:
    settings = gz.settings.load(hub.setting("gitzoid"))
    groups = {g: {"repos": grp.repos, "email_list": grp.email_list} for g, grp in settings.security_scope().items()}
    return {"groups": groups, "stamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}


@step(max_attempts=2)
def scan(repo: str) -> dict:
    """Advisories for one repo that pass the feed gate, with where the code
    imports each package. Raises nothing: a failure is a coverage problem."""
    settings, store, gh = flow.ctx()
    feeds = gz.runtime.make_feeds(settings, flow.HUB_DIR)
    notes = []
    try:
        root, sha = gz.checkout.ensure(gh, repo, hub.data_dir)
        skipped: list[str] = []
        manifests, all_listed = gz.security.manifest_files(root, exclude=settings.exclude_for(repo), skipped=skipped)
        deps, problems = gz.security.dependencies(manifests)
        if not all_listed:
            problems.append(f"more than {len(manifests)} dependency files; the rest were not checked")
        notes += problems
        try:
            kev = feeds.kev()
            store.coverage_ok(repo, "exploited-now list")
        except gz.security.FeedError as exc:
            kev = set()
            store.coverage_error(repo, "exploited-now list", str(exc))
            notes.append(str(exc))
        advisories = feeds.advisories(deps) if deps else []
    except Exception as exc:  # noqa: BLE001 - reported, never silent
        store.coverage_error(repo, "dependencies", f"{type(exc).__name__}: {exc}")
        return {"repo": repo, "error": f"{type(exc).__name__}: {exc}"[:300]}
    floor = settings.security.severity_floor
    direct = gz.security.declared(manifests)
    code, code_complete = gz.security.source_files(root)
    items, unused = [], 0
    for a in advisories:
        a["kev"] = bool(set(a["cves"]) & kev)
        if not gz.security.passes_gate(a, kev, floor):
            continue
        hits = gz.security.imports(code, a["dep"])
        if gz.security.unused(a["dep"], direct, hits, code_complete) and not a["kev"]:
            unused += 1
            continue
        a["usage"] = hits or []
        a["assessment_fp"] = gz.runtime.digest_of(sha, a["dep"], a["summary"], a.get("fixed"), a["usage"])
        if not hits:
            a["usage"] = ["No static import found. CLI, plugin, transitive and dynamic usage is unknown; do not infer unused."]
        a["assess"] = gz.security.needs_assessment(store, repo, a)
        items.append(a)
    if problems:
        store.coverage_error(repo, "lockfiles", "; ".join(problems))
    return {"repo": repo, "sha": sha, "deps": len(deps), "items": items, "unused": unused, "notes": notes,
            "complete": not problems, "profile": gz.profiles.text(store.profile(repo)), "skipped": skipped}


@step()
def security_record(repo: str, items: list[dict], ignored: list[dict], stamp: str, clean: bool, complete: bool,
                    skipped_files: list[str] = ()) -> dict:
    store = gz.store.Store(hub.data_dir)
    for it in ignored:
        gz.security.ignore(store, repo, it, it.get("impact") or "judged not reachable from this code", stamp)
    changes = gz.security.reconcile(store, repo, items, stamp, complete=complete, skipped_files=set(skipped_files))
    if clean:
        store.coverage_ok(repo, "dependencies")
        store.coverage_ok(repo, "lockfiles")
    return changes


@step()
def security_group_view(repos: list[str]) -> dict:
    store = gz.store.Store(hub.data_dir)
    return {"open": store.ledger_rows(repos, state="open"), "coverage": store.coverage(repos)}


@step()
def security_after_email(sent_ok: bool, keys: list[str], coverage: list[dict]) -> None:
    """An alert counts as given only once its email went out; otherwise the
    next run alerts again."""
    store = gz.store.Store(hub.data_dir)
    if not sent_ok:
        store.unalert(keys)
        return
    for c in coverage:
        store.coverage_alerted(c["repo"], c["check_name"])


@workflow(schedule="daily at 06:00", catch_up=True)
def security_watch():
    p = security_plan()
    settings = gz.settings.load(hub.setting("gitzoid"))
    out = {}
    for group, g in p["groups"].items():
        changes = {"new": [], "escalated": [], "resolved": []}
        summary = []
        for repo in g["repos"]:
            s = scan(repo)
            if s.get("error"):
                summary.append({"repo": repo, "error": s["error"]})
                continue
            items, ignored = s["items"], []
            todo = [i for i in items if i["assess"]]
            if todo and not flow.charge(1):
                try:
                    review = flow.ask(gz.security.assessment_prompt(repo, s["profile"], todo),
                                      system=gz.security.ASSESS_SYSTEM, response_model=gz.models.DependencyReview,
                                      model=settings.security.model)
                    verdicts = {v.advisory: v for v in review.items}
                except flow.Unreadable:
                    verdicts = {}  # unjudged advisories are kept: an alert is safer than silence
                kept = []
                for it in items:
                    v = verdicts.get(it["key"])
                    # The model may judge an advisory unreachable, but never one exploited now or critical.
                    if v is not None and not v.keep and not it["kev"] and it["severity"] != "critical":
                        ignored.append({**it, "impact": v.impact})
                        continue
                    if v is not None:
                        it = {**it, "impact": v.impact, "fix": v.fix}
                    kept.append(it)
                items = kept
            c = security_record(repo, items, ignored, p["stamp"], not s["notes"], s["complete"], s.get("skipped", []))
            for k in changes:
                changes[k] += [{**x, "repo": repo} for x in c[k]]
            summary.append({"repo": repo, "dependencies": s["deps"], "advisories": len(items),
                            "new": len(c["new"]), "worse": len(c["escalated"]), "resolved": len(c["resolved"]),
                            "unused_dropped": s["unused"], "notes": s["notes"]})
        view = security_group_view(g["repos"])
        problems = gz.runtime.due_coverage(view["coverage"])
        art, sent = None, None
        if changes["new"] or changes["escalated"] or problems:
            # One page per alert, never replaced: an emailed link always shows what was sent.
            html = gz.render.security_page(group, p["stamp"], changes, view["open"], view["coverage"])
            art = gz.runtime.publish(hub, f"security-{group}", html, f"Security · {group} · {p['stamp'][:10]}",
                                     key=None, team_audience="hub")
            subject, body = gz.mail.security_email(changes, problems, len(g["repos"]), gz.runtime.link_of(art),
                                                   repos=gz.runtime.pr_repos(settings))
            sent = gz.runtime.email_list(hub, g["email_list"], subject, body, art)
            security_after_email(gz.runtime.delivered(sent), [x["key"] for x in changes["new"] + changes["escalated"]],
                        problems)
        out[group] = {"repos": summary, "page": gz.runtime.link_of(art), "emailed": bool(sent),
                      "email_list": g["email_list"]}
    return out
