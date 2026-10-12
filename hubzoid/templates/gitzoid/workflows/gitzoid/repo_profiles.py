"""repo_profiles: what each repo is, its parts and where sign-in lives.

Built at setup. After that a profile is rebuilt only when the code it
describes changed: a default-branch push that touched a manifest, a route
file, sign-in or permission code, or the top-level layout. The daily check
compares each profile's commit with the branch; quiet repos cost no model
call. Anyone allowed can refresh one repo from chat ("refresh the profile of
acme/billing"); it asks for confirmation because it makes a model call.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

from pydantic import BaseModel, Field

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


class ProfileRequest(BaseModel):
    repo: str | None = Field(None, description="One repo (owner/name) to rebuild now. Empty: every repo whose profile "
                                               "is missing or out of date")


@step(max_attempts=2)
def profiles_plan(repo: str | None) -> dict:
    settings, store, gh = flow.ctx()
    if repo:
        if repo not in settings.repo_names():
            raise ValueError(f"{repo} is not one of GitZoid's repos: {', '.join(settings.repo_names())}")
        return {"build": [{"repo": repo, "reason": "requested"}], "fresh": []}
    build, fresh = [], []
    for name in settings.repo_names():
        prof = store.profile(name)
        if prof is None:
            build.append({"repo": name, "reason": "no profile yet"})
            continue
        if prof["stale"]:
            build.append({"repo": name, "reason": prof["stale_reason"] or "marked stale"})
            continue
        try:
            head = gh.branch_sha(name, gh.default_branch(name))
            if head == prof["sha"]:
                fresh.append(name)
                continue
            changed = gh.compare_files(name, prof["sha"], head)
            reason = "history rewritten" if changed is None else gz.profiles.relevant_change(changed, prof)
        except Exception as exc:  # noqa: BLE001 - check again tomorrow
            fresh.append(f"{name} (not checked: {type(exc).__name__})")
            continue
        if reason:
            build.append({"repo": name, "reason": reason})
        else:
            store.save_profile(name, head, prof["profile"])  # same profile, newer commit
            fresh.append(name)
    return {"build": build, "fresh": fresh}


@step(max_attempts=2)
def collect(repo: str) -> dict:
    settings, store, gh = flow.ctx()
    root, sha = gz.checkout.ensure(gh, repo, hub.data_dir)
    return {"sha": sha, "facts": gz.profiles.facts(root)}


@step()
def save(repo: str, sha: str, facts: dict, summary: dict) -> None:
    gz.store.Store(hub.data_dir).save_profile(repo, sha, {"facts": facts, "summary": summary})


@workflow(schedule="daily at 04:00", inputs=ProfileRequest, as_tool=True, confirm=True, catch_up=True,
          surfaces=("web", "owui", "mcp"))
def repo_profiles():
    """Rebuild a repo's profile (what it is, its parts, sign-in, data, sensitive areas) that reviews and chat use."""
    settings = gz.settings.load(hub.setting("gitzoid"))
    p = profiles_plan(hub.input.repo if hub.input else None)
    built = []
    for item in p["build"]:
        problem = flow.charge(1)
        if problem:
            built.append({**item, "error": problem})
            break
        try:
            data = collect(item["repo"])
        except Exception as exc:  # noqa: BLE001 - one unreachable repo never stops the others
            built.append({**item, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        try:
            summary = flow.ask(gz.profiles.prompt(item["repo"], data["facts"]), system=gz.profiles.SYSTEM,
                               response_model=gz.models.Profile, model=settings.review.model)
        except flow.Unreadable as exc:
            built.append({**item, "error": str(exc)})
            continue
        save(item["repo"], data["sha"], data["facts"], summary.model_dump())
        built.append({**item, "what_it_is": summary.what_it_is})
    lines = [f"{b['repo']}: " + (f"could not build ({b['error']})" if b.get("error") else f"rebuilt. {b['what_it_is']}")
             for b in built] + [r if "(" in r else f"{r}: already up to date" for r in p["fresh"]]
    return {"built": built, "up_to_date": p["fresh"], "answer": "\n".join(lines) or "No GitZoid repos to profile."}
