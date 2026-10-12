"""github_events: GitHub tells GitZoid about a push, a pull request or a
comment, and GitZoid acts within seconds.

Set up (README.md, "Instant reviews"): in workflows/settings.yaml,
`webhooks: {github: {verify: github}}` and `gitzoid: {trigger: webhook}`; in
the hub's .env, WEBHOOK_SECRET_GITHUB; in the repo or organisation settings,
a webhook to /webhooks/<hub>/github with that secret, for pull requests,
issue comments, pull request review comments and pushes.

Hubzoid checks GitHub's signature and drops repeated deliveries (by
X-GitHub-Delivery) before this runs. Up to four events run at once, one at a
time per repo.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
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
PR_ACTIONS = ("opened", "reopened", "synchronize", "ready_for_review", "labeled", "unlabeled")


def event_kind(body: dict) -> str:
    """GitHub's event type from the payload, for when the X-GitHub-Event
    header is not passed through."""
    if "zen" in body and "hook" in body:
        return "ping"
    if "comment" in body and "issue" in body:
        return "issue_comment"
    if "comment" in body and "pull_request" in body:
        return "pull_request_review_comment"
    if "pull_request" in body and "action" in body:
        return "pull_request"
    if "ref" in body and "commits" in body:
        return "push"
    return "unknown"


@step()
def mark_closed(repo: str, number: int) -> None:
    gz.store.Store(hub.data_dir).save_review(repo, number, state="closed")


@step(max_attempts=3)
def open_numbers(repo: str) -> list[int]:
    settings, store, gh = flow.ctx()
    return [p["number"] for p in gh.open_pulls(repo)][:settings.review.max_prs_per_run]


@workflow(on_webhook="github", concurrency=4, concurrency_key="repository.full_name")
def github_events():
    body = hub.event.body
    headers = getattr(hub.event, "headers", None) or {}
    kind = headers.get("x-github-event") or event_kind(body)
    if kind == "ping":
        return {"event": "ping", "ok": True}
    settings = gz.settings.load(hub.setting("gitzoid"))
    repo = (body.get("repository") or {}).get("full_name")
    if not repo or repo not in settings.repo_names():
        return {"event": kind, "ignored": f"{repo} is not one of GitZoid's repos"}
    owner = "event-" + (headers.get("x-github-delivery") or hashlib.sha1(repr(sorted(body)).encode()).hexdigest()[:12])
    if not flow.acquire(repo, owner, 600):
        return {"event": kind, "deferred": "another GitZoid run held this repo for 10 minutes; the next sweep catches up"}
    try:
        if kind == "pull_request":
            number, action = body["pull_request"]["number"], body.get("action")
            if action == "closed":
                mark_closed(repo, number)
                return {"event": kind, "action": action, "pr": number}
            if action not in PR_ACTIONS:
                return {"event": kind, "action": action, "ignored": "not a change GitZoid reviews"}
            return {"event": kind, "action": action, **flow.review_pr(repo, number)}
        if kind in ("issue_comment", "pull_request_review_comment"):
            if body.get("action") not in ("created", "edited"):
                return {"event": kind, "ignored": body.get("action")}
            if kind == "issue_comment":
                if "pull_request" not in (body.get("issue") or {}):
                    return {"event": kind, "ignored": "a comment on an issue, not a pull request"}
                comment = gz.github.comment_entry(body["comment"], "issue", body["issue"]["number"])
            else:
                comment = gz.github.comment_entry(body["comment"], "review", body["pull_request"]["number"])
            if "@gitzoid" not in comment["body"].lower():
                return {"event": kind, "ignored": "no @gitzoid"}
            return {"event": kind, **flow.run_command(repo, comment)}
        if kind == "push":
            default = (body.get("repository") or {}).get("default_branch")
            if body.get("ref") != f"refs/heads/{default}":
                return {"event": kind, "ignored": "not the default branch"}
            res = flow.on_push(repo, flow.push_files(body))
            if res["rules_changed"]:
                res["re_reviewed"] = [flow.review_pr(repo, n) for n in open_numbers(repo)]
            return {"event": kind, **res}
        return {"event": kind, "ignored": "not an event GitZoid uses"}
    finally:
        flow.release(repo, owner)
