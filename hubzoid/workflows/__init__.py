# Hubzoid workflows. MIT licensed like the rest of the repository.
"""Scheduled deterministic workflows on embedded DBOS, behind a thin façade.

A workflow runs a defined sequence of steps, including agent calls, to complete
recurring work. It coordinates steps, calls agents, retains state, retries
failures, and resumes after a restart — durable execution, with the engine (DBOS)
invisible behind `@workflow` / `@step` / `hub`.

Public surface (also re-exported from `hubzoid`):
    from hubzoid import workflow, step, hub

    @workflow(schedule="every 2 minutes")
    def review_prs():
        repo = hub.setting("repo")
        for pr in fetch_open_prs(repo):        # a @step; reads secrets inside
            if hub.state.get(f"done:{pr.id}") == pr.sha:
                continue
            review = hub.call_llm(prompt_for(pr))
            post_review(pr, review)            # your own @step side effect
            hub.state[f"done:{pr.id}"] = pr.sha

The `hub` proxy and durable state are usable and testable without DBOS; the
`@workflow`/`@step` decorators and the scheduler live in `runtime.py`.
"""
from __future__ import annotations

from .context import hub, run_scope
from .state import WorkflowState

__all__ = ["hub", "run_scope", "WorkflowState", "workflow", "step"]


def __getattr__(name):  # lazy: only import DBOS when the decorators are used
    if name in ("workflow", "step"):
        from . import runtime
        return getattr(runtime, name)
    raise AttributeError(name)
