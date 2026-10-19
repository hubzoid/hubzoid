"""Approvals and questions: the agent asks a person before it acts.

* ``config``  the optional ``<hub>/hitl.yaml`` (when to ask)
* ``store``   requests and grants in the operational store
* ``service`` the one place that creates, settles and continues requests
* ``gate``    the check around every tool the agent can call
* ``tools``   ``ask_question``
* ``routes``  ``/api/requests`` and ``/api/hitl/config``
* ``cli``     ``hubzoid requests`` and ``hubzoid hitl``
"""
from __future__ import annotations

from pathlib import Path


def equip(hub_dir: Path, registry: dict, *, questions: bool = True, agent: str | None = None) -> dict:
    """Wrap every tool in the approval gate and add ``ask_question`` (unless
    questions are off for this hub or agent, or the surface has nobody to ask).

    The access check per call (``access.guard.run_checked``) is the outermost
    layer: a run whose account lost the hub, or whose credential ended, is
    stopped before any approval card or question is raised."""
    from ..access import guard
    from . import gate, tools

    out = gate.apply(Path(hub_dir), registry)
    if questions and "ask_question" not in out:
        tool = tools.ask_question_tool(Path(hub_dir), agent=agent)
        if tool is not None:
            out["ask_question"] = tool
    return {name: guard.run_checked(ft, hub_dir) for name, ft in out.items()}
