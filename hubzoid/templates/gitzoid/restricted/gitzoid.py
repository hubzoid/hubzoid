"""GitZoid's chat and MCP tools for people allowed to use them (the `gitzoid`
capability, granted per agent in the Console). They read GitZoid's own data
and GitHub, never the hub's files. Hubzoid refuses restricted tools on Slack
and Telegram, so these answer in web chat and over MCP
from Claude Code or Codex.
"""
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
gz, HUB_DIR, _ctx, _gh, _repo, _safe = c.gz, c.HUB_DIR, c.ctx, c.gh, c.repo_name, c.safe


def _review_state(row: dict | None, live_head: str | None) -> str:
    if not row:
        return "not reviewed yet"
    if row.get("paused"):
        return f"paused ({row.get('pause_reason') or 'by a command'})"
    if row.get("state") in ("failed", "publishing"):
        return f"{row['state']}: {row.get('note') or 'review saved; publishing will retry'}"
    if live_head and row.get("reviewed_sha") == live_head:
        return "done: GitZoid has reviewed the latest push"
    if row.get("state") == "reviewing" and row.get("head_sha") == live_head:
        return "reviewing the latest push now"
    if row.get("state") == "skipped":
        return f"skipped: {row.get('note')}"
    if row.get("state") == "failed":
        return f"failed: {row.get('note')}"
    return "waiting: the latest push has not been reviewed yet"


@function_tool(strict_mode=False)
@_safe
def gitzoid_status(repo: str = "") -> str:
    """What GitZoid sees right now: pull requests waiting, under review, paused or with blocking findings,
    security items open, checks that could not run, and stale repo profiles. `repo` narrows it to one repo."""
    user, settings, store = _ctx()
    repos = [_repo(settings, repo)] if repo else settings.repo_names()
    out = []
    for name in repos:
        rows = [r for r in store.reviews(name) if r["state"] != "closed"]
        blocking = {}
        for r in rows:
            open_f = store.findings_for(name, r["number"], ("open",))
            n = sum(1 for f in open_f if gz.lifecycle.blocking(f))
            if n:
                blocking[r["number"]] = n
        ledger = store.ledger_rows([name], state="open")
        problems = [c for c in store.coverage([name]) if c.get("last_error")]
        prof = store.profile(name)
        out.append({
            "repo": name,
            "pull_requests": [{"number": r["number"], "title": r["title"], "author": r["author"],
                               "state": r["state"], "verdict": r["verdict"], "blocking_findings": blocking.get(r["number"], 0),
                               "paused": bool(r["paused"]), "note": r.get("note") or r.get("pause_reason"),
                               "reviewed_sha": r.get("reviewed_sha"), "published_sha": r.get("published_sha"), "url": r["url"]} for r in rows],
            "security_open": {s: sum(1 for x in ledger if x["severity"] == s) for s in ("critical", "high", "medium", "low", "unknown")
                              if any(x["severity"] == s for x in ledger)},
            "checks_not_running": [f"{c['check_name']}: {c['last_error']}" for c in problems],
            "profile": "missing" if not prof else ("stale: " + (prof["stale_reason"] or "") if prof["stale"] else "fresh"),
        })
    return json.dumps(out, indent=1)


@function_tool(strict_mode=False)
@_safe
def find_code(query: str, repo: str = "") -> str:
    """Where something is in the code: a regular expression or plain text, matched case-insensitively against
    file paths and lines of each repo's default branch. `repo` limits the search to one repo."""
    import hubzoid
    user, settings, store = _ctx()
    repos = [_repo(settings, repo)] if repo else settings.repo_names()
    gh = _gh(settings, store)
    hits = []
    for name in repos[:20]:
        root, _ = gz.checkout.ensure(gh, name, hubzoid.data_dir())
        hits += [f"{name}: {h}" for h in gz.checkout.grep(root, query, limit=40 - len(hits))]
        if len(hits) >= 40:
            break
    return "\n".join(hits) or f"No match for {query!r}."


@function_tool(strict_mode=False)
@_safe
def read_code(repo: str, path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Show a file from a repo's default branch with line numbers (up to 400 lines per call)."""
    import hubzoid
    user, settings, store = _ctx()
    name = _repo(settings, repo)
    root, _ = gz.checkout.ensure(_gh(settings, store), name, hubzoid.data_dir())
    return gz.checkout.read(root, path, start_line, end_line or None)


@function_tool(strict_mode=False)
@_safe
def pr_details(repo: str, number: int) -> str:
    """A pull request's GitZoid review: whether the latest push has been reviewed yet (waiting, reviewing,
    done, paused), the verdict, every open finding with its fix prompt, and what was fixed or dismissed.
    A fix loop waits on this until the latest push shows done."""
    user, settings, store = _ctx()
    name = _repo(settings, repo)
    pr = _gh(settings, store).pull(name, number)
    row = store.review(name, number)
    rows = store.findings_for(name, number)
    view = store.view(name, number) or {}
    open_f = [f for f in rows if f["state"] == "open"]
    return json.dumps({
        "pull_request": {"repo": name, "number": number, "title": (pr or {}).get("title"), "url": (pr or {}).get("url"),
                         "head": ((pr or {}).get("head_sha") or "")[:7], "state": (pr or {}).get("state")},
        "review": _review_state(row, (pr or {}).get("head_sha")),
        "verdict": gz.review.VERDICT.get((row or {}).get("verdict") or "", None),
        "summary": view.get("summary"),
        "status_check": settings.status_for(name),
        "publication": "preview" if not settings.review.post_to_github else (row or {}).get("state"),
        "reviewed_sha": (row or {}).get("reviewed_sha"), "published_sha": (row or {}).get("published_sha"),
        "coverage": {k: view.get(k) for k in ("reviewed_files", "total_files", "left_out", "built", "ignored", "truncated", "notes")},
        "blocking": sum(1 for f in open_f if gz.lifecycle.blocking(f)),
        "open_findings": [{"id": f["id"], "path": f["path"], "line": f["line"], "severity": f["severity"],
                           "category": f["category"], "rule": f["rule"], "body": f["body"],
                           "fix_prompt": gz.review.fix_prompt(f, {"repo": name, "number": number})} for f in open_f],
        "fixed": [{"path": f["path"], "body": f["body"]} for f in rows if f["state"] == "fixed"],
        "dismissed_or_deferred": [{"path": f["path"], "body": f["body"], "state": f["state"], "by": f["by_user"],
                                   "reason": f["reason"]} for f in rows if f["state"] in gz.lifecycle.SUPPRESSED],
    }, indent=1)


@function_tool(strict_mode=False)
@_safe
def findings(repo: str = "", state: str = "open", rule: str = "", limit: int = 30) -> str:
    """GitZoid findings across pull requests, newest first. `state`: open, fixed, dismissed, deferred, outdated
    or empty for all. `rule` matches the team rule name."""
    user, settings, store = _ctx()
    name = _repo(settings, repo) if repo else None
    rows = store.search_findings(name, state or None, rule or None, max(1, min(limit, 100)))
    return json.dumps([{"repo": f["repo"], "pr": f["number"], "path": f["path"], "line": f["line"],
                        "severity": f["severity"], "category": f["category"], "rule": f["rule"], "state": f["state"],
                        "body": f["body"], "reason": f["reason"]} for f in rows], indent=1) if rows else "None."


@function_tool(strict_mode=False)
@_safe
def history(repo: str, path: str = "", days: int = 30) -> str:
    """What changed in a repo, or in one path, over the last `days` days: commits with their pull request numbers."""
    user, settings, store = _ctx()
    name = _repo(settings, repo)
    gh = _gh(settings, store)
    rows = gh.commits(name, path or None, gh.now() - timedelta(days=max(1, min(days, 365))), limit=40)
    return "\n".join(f"{c['date'][:10]} {c['sha'][:7]} {c['author']}: {c['message'].splitlines()[0]}" for c in rows) \
        or "No commits in that window."


@function_tool(strict_mode=False)
@_safe
def security(query: str = "", repo: str = "") -> str:
    """Are we exposed? `query` is a package name, a CVE or advisory id, or empty for everything open. Answers
    from GitZoid's security ledger and from the packages each repo installs today."""
    import hubzoid
    user, settings, store = _ctx()
    repos = [_repo(settings, repo)] if repo else settings.repo_names()
    q = query.strip().lower()
    ledger = [r for r in store.ledger_rows(repos, state=None) if r["state"] in ("open", "ignored")
              and (not q or q in (r.get("package") or "").lower() or q in (r.get("advisory") or "").lower()
                   or q in (r.get("title") or "").lower())]
    installed = []
    if q and not q.startswith(("cve-", "ghsa-", "pysec-")):
        for name in repos[:20]:
            root = gz.checkout.path_for(hubzoid.data_dir(), name)
            if root.is_dir():
                deps, _ = gz.security.dependencies(gz.security.manifest_files(root)[0])
                installed += [f"{name}: {d['name']} {d['version']} ({d['file']})" for d in deps if q in d["name"].lower()]
    out = {"ledger": [{"repo": r["repo"], "kind": r["kind"], "package": r["package"], "version": r["version"],
                       "advisory": r["advisory"], "severity": r["severity"], "exploited_now": bool(r["kev"]),
                       "state": "open" if r["state"] == "open" else "judged not reachable", "impact": r["impact"],
                       "fix": r["fix"], "where": r["path"]} for r in ledger[:50]],
           "installed_versions": installed[:50],
           "coverage_problems": [f"{c['repo']} {c['check_name']}: {c['last_error']}" for c in store.coverage(repos)
                                 if c.get("last_error")]}
    return json.dumps(out, indent=1)


@function_tool(strict_mode=False)
@_safe
def repo_profile(repo: str) -> str:
    """What a repo is: its parts, where sign-in and permission checks live, its data and sensitive areas."""
    user, settings, store = _ctx()
    name = _repo(settings, repo)
    prof = store.profile(name)
    if not prof:
        return f"No profile for {name} yet. It is built by the repo_profiles workflow."
    return json.dumps({"repo": name, "commit": (prof["sha"] or "")[:7], "built": prof["built_at"],
                       "stale": prof["stale_reason"] if prof["stale"] else None, **prof["profile"].get("summary", {}),
                       "routes": prof["profile"].get("facts", {}).get("routes", [])[:30]}, indent=1)


@function_tool(strict_mode=False)
@_safe
def rules_for(path: str, repo: str = "") -> str:
    """The team rules (knowledge/review-rules/) and repo rules (review.md) that apply to a file path."""
    user, settings, store = _ctx()
    hub_rules = gz.rules.rules_for(gz.rules.hub_rules(HUB_DIR), path)
    out = {"team_rules": [{"title": r["title"], "file": r["file"], "paths": r["paths"], "severity": r["severity"],
                           "rule": r["text"]} for r in hub_rules]}
    if repo:
        name = _repo(settings, repo)
        controls = gz.rules.repo_controls(_gh(settings, store), name)
        out["repo_rules"] = {"file": controls.get("review_md"), "focus": controls.get("focus"),
                             "ignored_here": any(gz.rules.match_glob(path, g) for g in controls.get("ignore", [])),
                             "for_this_path": {g: t for g, t in controls.get("path_instructions", {}).items()
                                               if gz.rules.match_glob(path, g)},
                             "instructions": controls.get("instructions")}
    return json.dumps(out, indent=1)


@function_tool(strict_mode=False)
@_safe
def propose_rule(title: str, rule: str, paths: str = "") -> str:
    """Draft a team review rule for a hub admin to approve. A draft never affects a review until approved.
    `paths` is optional, comma-separated globs such as app/** or *.py."""
    user, settings, store = _ctx()
    if len(title.strip()) < 3 or len(rule.strip()) < 10:
        raise ValueError("Give the rule a short title and a sentence or two saying what to check.")
    globs = [p.strip() for p in paths.split(",") if p.strip()][:10]
    n = store.add_draft(title.strip()[:120], rule.strip()[:3000], globs, getattr(user, "email", None))
    return f"Drafted rule #{n} \"{title.strip()}\". It applies once a hub admin approves it."
