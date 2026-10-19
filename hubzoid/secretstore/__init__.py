"""The secret store and the one secret reference grammar (GitHub #49).

Other parts of Hubzoid use three things from here:

* ``parse(text)`` turns ``secret:<scope>/<name>`` or ``env:<NAME>`` into a
  ``Ref``, raising ``RefError`` with a message that says what to write.
* ``resolve(ref, hub_dir)`` returns the value a reference names, for trusted
  connector and tool code only. It never logs the value.
* ``hubzoid secrets set|list|delete`` and ``/api/hubs/{hub_id}/secrets`` (or
  ``/api/secrets`` for the deployment) write values, so a feature never adds
  its own secret table.

Values are write-only: no route or command returns one. Stored secrets whose
names are environment variable names also join the environment when a bridge
starts, below every operator source (``layer``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from .refs import Ref, RefError, from_object, parse

__all__ = ["Ref", "RefError", "SecretError", "from_object", "parse", "resolve"]


def __getattr__(name: str):
    # The store pulls in the database stack. Load it only when used, so the CLI
    # and the run supervisor stay light.
    if name == "SecretError":
        from .store import SecretError

        return SecretError
    raise AttributeError(name)


def resolve(ref: str | Ref, hub_dir, *, env: Mapping[str, str] | None = None) -> str:
    """The value ``ref`` names for this hub.

    ``env:`` reads the layered configuration (``env``, default the process
    environment). ``secret:`` reads the store. A hub or channel reference must
    name this hub. Raises SecretError ``secret_unavailable`` when nothing is
    set, ``secret_unreadable`` when the deployment key cannot decrypt it, and
    ``permission_denied`` for another hub's secret."""
    from ..assertions import hub_key
    from . import store
    from .store import SecretError

    parsed = parse(ref) if isinstance(ref, str) else ref
    if parsed.provider == "env":
        source = os.environ if env is None else env
        value = (source.get(parsed.name) or "").strip()
        if not value:
            raise SecretError("secret_unavailable", f"{parsed.name} is not set. Set it with hubzoid "
                              "secrets set, or in the environment.", 503)
        return value
    if parsed.scope in ("hub", "channel") and parsed.hub != hub_key(Path(hub_dir)):
        raise SecretError("permission_denied", "A hub can read only its own secrets.", 403)
    value = store.value(hub_dir, scope=parsed.scope, target=parsed.target, name=parsed.name)
    if not value:
        raise SecretError("secret_unavailable", f"No secret is stored for {parsed}. Set it with "
                          "hubzoid secrets set.", 503)
    return value
