"""Approving team rules (the `gitzoid_admin` capability, for hub admins).
`@gitzoid` cannot change rules and drafts never affect a review: a rule
changes only when an admin approves a draft here, which writes the rule file
in knowledge/review-rules/, or when someone edits those files directly."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
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
def rule_drafts() -> str:
    """Team rules drafted from chat or MCP and waiting for an admin's decision."""
    user, settings, store = c.ctx()
    rows = store.drafts("pending")
    return json.dumps([{"id": d["id"], "title": d["title"], "paths": d["paths"], "rule": d["text"],
                        "proposed_by": d["proposed_by"], "at": d["created_at"]} for d in rows], indent=1) \
        if rows else "No drafts are waiting."


@function_tool(strict_mode=False)
@c.safe
def decide_rule(draft_id: int, decision: str, note: str = "") -> str:
    """Approve or reject a drafted team rule. `decision` is approve or reject. Approval writes the rule file,
    and the next review follows it."""
    user, settings, store = c.ctx()
    d = store.draft(draft_id)
    if not d:
        raise ValueError(f"No draft #{draft_id}.")
    if d["state"] != "pending":
        raise ValueError(f"Draft #{draft_id} was already {d['state']}.")
    who = getattr(user, "email", None)
    if decision.strip().lower() in ("approve", "approved", "yes"):
        path = gz.rules.write_rule(c.HUB_DIR, d["title"], d["text"], d["paths"], who)
        store.decide_draft(draft_id, "approved", who, note, path)
        return f"Approved. The rule is now {path}; open pull requests it applies to are reviewed again."
    if decision.strip().lower() in ("reject", "rejected", "no"):
        store.decide_draft(draft_id, "rejected", who, note)
        return f"Rejected draft #{draft_id}."
    raise ValueError("decision must be approve or reject")
