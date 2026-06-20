"""
SDK-agnostic enforcement point. Depends on the authz INTERFACE, never on a vendor.
Both the Claude Agent SDK and the OpenAI Agents SDK call this from the shared tool
dispatch, so one rule covers both backends.
"""
from __future__ import annotations
import os
import json
import datetime
from authz import AccessDenied

AUDIT_PATH = os.path.join(os.path.dirname(__file__), "audit_log.jsonl")


def _audit(user, action, resource, decision):
    with open(AUDIT_PATH, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "user": user, "action": action, "resource": resource, "decision": decision,
        }) + "\n")


def guarded_tool_call(authz, user, tool, fn, args):
    """
    authz is any object with .check(user, action, resource). It could be the mock,
    Casbin, OpenFGA, or anything else. This function does not know or care which.
    """
    allowed = authz.check(user, "execute", tool)
    _audit(user, "execute", tool, "allow" if allowed else "deny")
    if not allowed:
        raise AccessDenied(f"{user} is not permitted to run '{tool}'")
    return fn(**args)
