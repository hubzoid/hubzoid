"""pr_sweep: every 2 minutes, review pull requests with new commits and run
new @gitzoid commands. With `trigger: poll` (the default, no public URL
needed) this is how GitZoid works. With `trigger: webhook` it runs a full
sweep every 30 minutes only, as a safety net for missed deliveries.

Listing pull requests uses ETags, so an unchanged repo costs nothing against
GitHub's rate limit. Existing eligible open pull requests are queued by default. `review.backlog`
selects all, latest (first_run_limit) or new-only behavior. At most `review.max_prs_per_run` reviews per sweep.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
import uuid
from datetime import timedelta
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
SAFETY_SWEEP = timedelta(minutes=30)


@step(max_attempts=2)
def sweep_plan() -> dict:
    settings, store, gh = flow.ctx()
    now = gh.now()
    if settings.trigger == "webhook":
        last = store.cursor("sweep:last")
        if last and last["value"] and now - gz.github.ts(last["value"]) < SAFETY_SWEEP:
            return {"skip": "webhook mode: the safety sweep runs every 30 minutes"}
    store.save_cursor("sweep:last", now.isoformat())
    hub_rules, guide = gz.rules.hub_rules(flow.HUB_DIR), gz.rules.guide(flow.HUB_DIR)
    repos, errors = [], []
    for repo in settings.repo_names():
        try:
            pulls = gh.open_pulls(repo)
            repo_controls = flow.controls(gh, store, repo)
            version = flow.configuration_version(settings, repo, repo_controls, hub_rules, guide, store.profile(repo))
            rows = {r["number"]: r for r in store.reviews(repo)}
            first = store.cursor(f"seen:{repo}") is None
            review, taken = [], 0
            for p in sorted(pulls, key=lambda p: p["created_at"] or "", reverse=True):
                row = rows.get(p["number"])
                if row and row["paused"]:
                    continue
                reason = gz.rules.skip_reason(p, settings.review, repo_controls)
                if reason:  # drafts, bots, the skip label: recorded, and never count toward the first-run limit
                    if not (row and row["state"] == "skipped" and row["note"] == reason):
                        store.save_review(repo, p["number"], title=p["title"], author=p["author"], url=p["url"],
                                          head_sha=p["head_sha"], state="skipped", rules_version=version, note=reason)
                    continue
                if row and row["head_sha"] == p["head_sha"] and row["rules_version"] == version and (
                        row["state"] == "done" or (row["state"] == "skipped" and (
                            (settings.review.backlog != "all" and (row["note"] or "").startswith("backlog")) or
                            (settings.review.max_age_days and (row["note"] or "").startswith("opened over"))))):
                    continue  # publishing, failed, or no longer skipped: picked up again
                if first and settings.review.backlog != "all" and taken >= (settings.review.first_run_limit if settings.review.backlog == "latest" else 0):
                    store.save_review(repo, p["number"], title=p["title"], author=p["author"], url=p["url"],
                                      head_sha=p["head_sha"], state="skipped", rules_version=version,
                                      note=("backlog: existing PR; new-only mode waits for its next push or manual review"
                                            if settings.review.backlog == "new" else
                                            f"backlog: outside the {settings.review.first_run_limit} newest on "
                                            "GitZoid's first run; reviewed after its next push or manual review"))
                    continue
                created = gz.github.ts(p["created_at"])
                if not row and settings.review.max_age_days and created and created < now - timedelta(days=settings.review.max_age_days):
                    store.save_review(repo, p["number"], title=p["title"], author=p["author"], url=p["url"],
                                      head_sha=p["head_sha"], state="skipped", rules_version=version,
                                      note=f"opened over {settings.review.max_age_days} days ago; waits for a push or manual review")
                    continue
                taken += 1
                if row is None or row["state"] in ("skipped", "closed") or row["head_sha"] != p["head_sha"]:
                    store.save_review(repo, p["number"], title=p["title"], author=p["author"], url=p["url"],
                                      head_sha=p["head_sha"], state="waiting", rules_version=version,
                                      note="Queued for review; the per-run budget controls when it starts")
                review.append(p["number"])
            open_now = {p["number"] for p in pulls}
            for number, row in rows.items():
                if number not in open_now and row["state"] != "closed":
                    store.save_review(repo, number, state="closed")
            comments, cursor = [], store.cursor(f"comments:{repo}")
            since = cursor["value"] if cursor and cursor["value"] else now.replace(microsecond=0).isoformat()
            newest = since
            if settings.commands_for(repo):
                found = gh.issue_comments_since(repo, since) + gh.review_comments_since(repo, since)
                for c in sorted(found, key=lambda c: c["updated_at"] or ""):
                    newest = max(newest, c["updated_at"] or newest)
                    done = store.command(gz.commands.key(repo, c))
                    if "@gitzoid" in c["body"].lower() and not (done and done["outcome"]):
                        comments.append(c)
            repos.append({"repo": repo, "review": review, "comments": comments, "cursor": newest, "first": first})
        except Exception as exc:  # noqa: BLE001 - one unreachable repo never stops the others
            errors.append({"repo": repo, "error": f"{type(exc).__name__}: {exc}"[:300]})
    skipped = [{"pr": f"{r['repo']}#{r['number']}", "reason": r.get("note") or r.get("pause_reason")}
               for r in store.reviews() if r["repo"] in settings.repo_names() and r["state"] in ("skipped", "paused")]
    return {"skipped": skipped, "repos": repos, "budget": settings.review.max_prs_per_run, "owner": f"sweep-{uuid.uuid4().hex[:12]}",
            "errors": errors}


@step()
def finish(repo: str, cursor: str, first: bool) -> None:
    store = gz.store.Store(hub.data_dir)
    store.save_cursor(f"comments:{repo}", cursor)
    if first:
        store.save_cursor(f"seen:{repo}", True)


@workflow(schedule="every 2 minutes")
def pr_sweep():
    p = sweep_plan()
    if "skip" in p:
        return p
    out = {"skipped": p.get("skipped", []), "reviews": [], "commands": [], "busy": [], "deferred": [], "errors": list(p["errors"])}
    budget = p["budget"]
    for r in p["repos"]:
        repo = r["repo"]
        if not flow.acquire(repo, p["owner"], 0):
            out["busy"].append(repo)
            continue
        try:
            for c in r["comments"]:
                out["commands"].append(flow.run_command(repo, c))
            for number in r["review"]:
                if budget <= 0:
                    out["deferred"].append(f"{repo}#{number}")
                    continue
                if not flow.acquire(repo, p["owner"], 0):  # renew the lease before each review
                    out["busy"].append(repo)
                    break
                try:
                    res = flow.review_pr(repo, number)
                except Exception as exc:  # noqa: BLE001 - one pull request never stops the others
                    if flow.account_lost(exc):
                        raise
                    res = {"pr": f"{repo}#{number}", "failed": f"{type(exc).__name__}: {exc}"[:300]}
                    flow.fail(repo, number, res["failed"])
                if "skipped" not in res:
                    budget -= 1
                out["reviews"].append(res)
            finish(repo, r["cursor"], r["first"])
        except Exception as exc:  # noqa: BLE001 - recorded; the next sweep retries
            if flow.account_lost(exc):
                raise
            out["errors"].append({"repo": repo, "error": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            flow.release(repo, p["owner"])
    if out["errors"] and not (out["reviews"] or out["commands"]):
        raise RuntimeError("; ".join(f"{e['repo']}: {e['error']}" for e in out["errors"]))
    return out
