"""Check GitZoid setup without a model call or any GitHub writes."""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from hubzoid import hub, step, workflow

path = Path(__file__).with_name("_flow.py")
name = "gitzoid_flow_" + hashlib.sha1(str(path).encode()).hexdigest()[:10]
if name not in sys.modules:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
flow = sys.modules[name]


@step()
def check() -> dict:
    settings, store, gh = flow.ctx()
    repos = []
    for repo in settings.repo_names():
        try:
            branch = gh.default_branch(repo)
            sha = gh.branch_sha(repo, branch)
            pulls = gh.open_pulls(repo)
            controls = flow.controls(gh, store, repo, fresh=True)
            eligible = [p for p in pulls if not flow.gz.rules.skip_reason(p, settings.review, controls)]
            ci = gh.ci_status(repo, sha)
            # Read permissions are exercised. Write permissions are not tested by mutating a repo.
            identity = gh.login()
            repos.append({"repo": repo, "ready": True, "identity": identity, "default_branch": branch,
                          "open": len(pulls), "eligible": len(eligible), "ci": ci,
                          "profile": "ready" if store.profile(repo) else "run repo_profiles next"})
        except Exception as exc:
            repos.append({"repo": repo, "ready": False, "error": f"{type(exc).__name__}: {exc}"[:300]})
    from hubzoid.access import store_for
    subject = hub.user.email or hub.user.id
    access = store_for(flow.HUB_DIR).can(subject, hub.name, "gitzoid")
    return {"chat_tools": "enabled for " + subject if access else
            "Not enabled for " + subject + "; choose GitZoid tools in Console → Access, or use hubzoid grant",
            "source": settings.source, "repos": repos,
            "ready": bool(repos) and all(r["ready"] for r in repos),
            "publishing": "live" if settings.review.post_to_github else "preview; nothing written to GitHub",
            "backlog": settings.review.backlog, "reviews_per_sweep": settings.review.max_prs_per_run,
            "model": "uses MODEL in the Hub .env unless a workflow override is selected; not tested by this check",
            "next": "Run repo_profiles, then pr_sweep. Open the results in Console. Start hubzoid run to keep schedules active.",
            "write_permissions": "not tested; posting requires Pull requests and Issues write, Commit statuses write",
            "email": "optional; no email is sent by this check"}


@workflow()
def setup_check():
    return check()
