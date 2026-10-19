"""Core-shipped hub memory and hub change tools, gated by ``curator``.

Every hub has them, but the caller needs the ``curator`` grant (shown in the
Console as Save shared knowledge), or manages the hub, on an allowed surface.
Installing the tools grants nothing.

* ``remember`` saves something for everyone who uses the hub, ONLY when a
  person explicitly asks. It edits the topic's file under
  ``knowledge/_learned/`` with an exact edit (a new file only for a new topic).
* ``read_hub_file``, ``propose_hub_change`` and ``hub_change_status`` let a
  person change the hub's instructions and knowledge from chat.

All of them go through ``hubzoid.hub_changes``: a hub manager's change to
knowledge/ applies at once; anyone else's is a proposal reviewed in Settings,
Hub changes, then written and committed exactly. Knowledge reads are
disk-live, so an applied change is used by the very next call.
"""
from __future__ import annotations

from ..capabilities import Capability, register

# The capability a caller needs, registered here where it is enforced so the
# Console row and the guard share one id.
CURATOR = register(Capability(
    permission="curator", label="Save shared knowledge", group="tools",
    description="Use remember and propose changes to this agent's instructions and knowledge.",
    surfaces=("chat", "workflow"),
))
CURATOR_PERMISSION = CURATOR.permission


def make(ctx) -> list:
    from ..hub_changes.tools import make as make_tools

    return make_tools(ctx.hub_dir)
