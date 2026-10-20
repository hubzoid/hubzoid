"""review_diff: a GitZoid review of local changes before they are pushed, from
Claude Code or Codex over MCP ("review my diff with gitzoid"). Same team
rules, repo rules, checks and gate as a pull request review. Nothing is
posted anywhere; the findings come back to the person who asked, inline
(Hubzoid shows the run's "answer" text after the tool call).

The pull request review later also checks each finding against the whole
file; this pre-check has only the diff, so it may keep a finding that the
full review would refute.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

from pydantic import BaseModel, Field

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


flow = _load("_flow.py")
gz = flow.gz


class DiffReview(BaseModel):
    repo: str = Field(description="The repo the change is for (owner/name), so its rules apply")
    diff: str = Field(max_length=200_000, description="Output of `git diff` (unified), up to 200 KB")


@step(max_attempts=2)
def diff_prepare(repo: str, diff_text: str) -> dict:
    settings, store, gh = flow.ctx()
    if repo not in settings.repo_names():
        raise ValueError(f"{repo} is not one of GitZoid's repos: {', '.join(settings.repo_names())}")
    files = gz.diff.parse_unified(diff_text)
    if not files:
        raise ValueError("That is not a unified diff. Send the output of `git diff`.")
    repo_controls = flow.controls(gh, store, repo)
    keep, ignored = gz.rules.reviewable(files, repo_controls)
    hub_rules = gz.rules.hub_rules(flow.HUB_DIR)
    rules_now = [r for r in hub_rules if any(gz.rules.rules_for([r], f["filename"]) for f in keep)]
    built: list[str] = []
    text, left_out = gz.diff.format_diff(keep, settings.review.max_diff_chars, built=built)
    sweep = gz.checks.sweep(keep)
    paths = {f["filename"] for f in keep}
    pr = {"repo": repo, "number": 0, "title": "Local changes (not pushed)", "body": "", "author": "you",
          "head_ref": "local"}
    return {"pr": pr, "controls": repo_controls, "guide": gz.rules.guide(flow.HUB_DIR),
            "team_context": "\n".join(x for x in (settings.context, settings.repo(repo).context) if x),
            "rules": [{k: r[k] for k in ("title", "paths", "text")} for r in rules_now],
            "path_instructions": {g: t for g, t in repo_controls.get("path_instructions", {}).items()
                                  if any(gz.rules.match_glob(p, g) for p in paths)},
            "profile": gz.profiles.text(store.profile(repo)), "checks": [c for c in sweep if not c["secret"]],
            "code_findings": [{**f, "checked": True} for f in gz.checks.secret_findings(sweep)
                              + gz.rules.code_checks(rules_now, keep)],
            "diff": text, "left_out": left_out, "built": built, "ignored": ignored, "incremental": False,
            "commentable": sorted(gz.diff.commentable(keep)),
            "floor": settings.repo(repo).severity_floor or repo_controls.get("severity_floor") or settings.review.severity_floor,
            "max_findings": settings.review.max_findings, "model": settings.model_for(repo),
            "files": len(keep), "discussion": [], "decided": [], "open": []}


@workflow(inputs=DiffReview, as_tool=True, tool_wait="3m", max_input_kb=256, surfaces=("web", "owui", "mcp"))
def review_diff():
    """Review local changes (the output of `git diff`) before they are pushed:
    the same team rules, checks and gate as a pull request review. Returns the
    findings inline; nothing is posted."""
    try:
        job = diff_prepare(hub.input.repo, hub.input.diff)
    except ValueError as exc:
        return {"refused": str(exc)}
    problem = flow.charge(1)
    if problem:
        out = {"repo": job["pr"]["repo"], "error": problem}
        return {**out, "answer": gz.review.diff_answer(out)}
    try:
        review = flow.ask(gz.review.review_prompt(job), system=gz.review.system_prompt(job["guide"], job["team_context"]),
                          response_model=gz.models.Review, model=job["model"]).model_dump()
    except flow.Unreadable as exc:
        out = {"repo": job["pr"]["repo"], "error": str(exc)}
        return {**out, "answer": gz.review.diff_answer(out)}
    kept = gz.review.gate(job["code_findings"] + review["findings"], {tuple(x) for x in job["commentable"]},
                          job["floor"], job["max_findings"])
    findings = [{"path": f["path"], "line": f["line"], "severity": f["severity"], "category": f["category"],
                 "body": f["body"], "rule": f["rule"], "fix_prompt": gz.review.fix_prompt(f, job["pr"])} for f in kept]
    out = {"repo": job["pr"]["repo"], "summary": review["summary"], "findings": findings,
           "files_reviewed": job["files"] - len(job["left_out"]) - len(job.get("built", [])),
           "left_out_for_size": job["left_out"], "built_output": job.get("built", []),
           "ignored_by_review_md": job["ignored"],
           "note": "Nothing was posted. The pull request review also checks each finding against the whole file."}
    return {**out, "answer": gz.review.diff_answer(out)}
