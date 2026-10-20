"""One hub process's drain state: the barrier a restart puts up first.

`hubzoid restart <hub>` asks the hub's bridge to drain (``POST
/internal/drain``). From then on nothing new starts in this process: chat
replies, `/v1` turns, MCP tool calls, approval continuations, 2.0 channel
routing and scheduled runs all check `active()` where they begin, and leave
their work saved for after the restart (queued messages stay queued, accepted
deliveries stay accepted, decided approvals stay to be continued). Work already
running finishes, with its leases and saves, until the supervisor's grace
period ends and the process stops.

The state is per process, because a hub is one process. It is never cleared:
a drained process is about to stop.
"""
from __future__ import annotations

import threading
import time

MESSAGE = "This agent is restarting. Try again in a moment."
INTERRUPTED = "The agent restarted during this reply."

_lock = threading.Lock()
_since: float | None = None


def start() -> float:
    """Put up the barrier (idempotent). Returns when it went up."""
    global _since
    with _lock:
        if _since is None:
            _since = time.time()
        return _since


def active() -> bool:
    return _since is not None


def since() -> float | None:
    return _since


def _reset_for_tests() -> None:
    global _since
    with _lock:
        _since = None
