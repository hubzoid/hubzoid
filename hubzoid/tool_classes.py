"""What each tool can change: one local class per tool, on every runtime.

A class describes the effect of calling a tool, not who may call it. Access
grants still decide who may call it, and write modes (`hubzoid.work_items`)
still decide whether a consequential action is offered at all.

| Class | Meaning |
|---|---|
| ``read`` | Reads only. It can still disclose private data or cost money. |
| ``sandbox_local`` | Runs inside the agent's own sandbox and changes only it. |
| ``source_push`` | Publishes source code, such as a git push. |
| ``handoff`` | Calls another agent or passes it a document. |
| ``external_write`` | Changes something outside the hub, or shared operational state. |
| ``shared_knowledge`` | Changes the hub's shared files or learned knowledge. |
| ``personal_store`` | Stores something owned by the person, such as a report. |

A tool nobody classified is an ``external_write``. Nothing a remote server says
about its own tools can make one a ``read``. Pure module, no runtime imports.
"""
from __future__ import annotations

from typing import Any

CLASSES = ("read", "sandbox_local", "source_push", "handoff", "external_write",
           "shared_knowledge", "personal_store")
UNCLASSIFIED = "external_write"

#: The pre-shipped tools, classified once here.
BUILTIN: dict[str, str] = {
    **{name: "read" for name in (
        "current_time", "explain_access", "grep_data", "http_get", "list_artifacts",
        "list_files", "list_knowledge", "list_skills", "list_workflows", "load_skill",
        "my_management_scope", "my_reports", "my_schedules", "read_file", "read_knowledge",
        "read_upload", "read_upload_full", "render_jinja", "web_search", "who_has_access",
        "workflow_runs")},
    **{name: "personal_store" for name in (
        "write_artifact", "publish_artifact", "schedule_workflow", "change_schedule",
        "propose_access_change", "propose_new_account")},
    "remember": "shared_knowledge",
    "write_hub_file": "shared_knowledge",
    "run_git": "source_push",
    **{name: "external_write" for name in (
        "send_email", "run_workflow", "pause_workflow", "resume_workflow",
        "cancel_workflow_run")},
}

_CLASS_ATTR = "hz_class"
_ACTION_ATTR = "hz_action"


def check(cls: str) -> str:
    if cls not in CLASSES:
        raise ValueError(f"Unknown tool class {cls!r}. Use one of: {', '.join(CLASSES)}.")
    return cls


def declare(tool: Any, cls: str, *, action: str | None = None) -> Any:
    """Record a tool's class (and, for a consequential write, the registered
    action it performs, such as ``email.send``). Returns the tool."""
    setattr(tool, _CLASS_ATTR, check(cls))
    if action is not None:
        setattr(tool, _ACTION_ATTR, action)
    return tool


def copy_tool(tool: Any, **changes) -> Any:
    """``dataclasses.replace(tool, **changes)`` that keeps what was recorded on
    the tool object itself (its class and action here, a hub tool's source, other
    markers): a wrapper must never turn a declared read into an external write.
    Pure: no runtime imports."""
    import dataclasses

    new = dataclasses.replace(tool, **changes)
    names = {f.name for f in dataclasses.fields(tool)}
    for key, value in vars(tool).items():
        if key not in names and key not in vars(new):
            try:
                setattr(new, key, value)
            except Exception:  # noqa: BLE001 - a frozen copy keeps only its fields
                pass
    return new


def classify(tool: Any) -> str:
    """The tool's class: what it declared, else the built-in table, else
    ``external_write``. Accepts a tool object or a tool name."""
    if isinstance(tool, str):
        return BUILTIN.get(tool, UNCLASSIFIED)
    declared = getattr(tool, _CLASS_ATTR, None)
    if declared in CLASSES:
        return declared
    return BUILTIN.get(getattr(tool, "name", ""), UNCLASSIFIED)


def action_of(tool: Any) -> str | None:
    """The consequential action a tool performs, when it declared one."""
    return getattr(tool, _ACTION_ATTR, None)
