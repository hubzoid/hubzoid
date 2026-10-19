"""Member chat features that need a grant (B037: off for members by default).

Each is an ordinary capability in the access catalogue, so the Console's
access screen, ``hubzoid grant`` and the access API grant and revoke it like
any tool. Owners and hub administrators (``manage_access``) have them all;
members only when granted. ``for_person`` is what ``/api/chat/capabilities``
reports, and the server checks the same rule wherever a feature runs.

* ``chat_dictation``: speak into the composer (the browser's own speech
  recognition; it fills the draft, never sends).
* ``chat_read_aloud``: hear an answer (the browser's own speech synthesis).
* ``chat_web_search``: let the agent search the public web for this person.
* ``chat_model_compare``: run the same turn on more than one model.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .. import capabilities as caps

log = logging.getLogger("hubzoid.chat.features")

FEATURES = {
    "chat_dictation": ("Dictation", "Speak into the message box. The browser turns speech into text; "
                                    "nothing is sent until the person sends it."),
    "chat_read_aloud": ("Read aloud", "Have answers read out by the browser's own voice."),
    "chat_web_search": ("Web search", "Let the agent search the public web while answering this person."),
    "chat_model_compare": ("Compare models", "Run the same request on more than one model side by side."),
}

for _perm, (_label, _desc) in FEATURES.items():
    caps.register(caps.Capability(permission=_perm, label=_label, group="tools", description=_desc,
                                  surfaces=("chat",)))


def for_person(hub_dir: Path, email: str) -> dict[str, bool]:
    """Which member features this person may use in this hub."""
    from ..access import store_for
    from ..access.identity import normalize

    email = normalize(email)
    hub = Path(hub_dir).name
    try:
        gs = store_for(Path(hub_dir))
        manager = bool(email) and gs.can(email, hub, "manage_access")
        return {perm: manager or (bool(email) and gs.can(email, hub, perm)) for perm in FEATURES}
    except Exception:  # noqa: BLE001 - fail closed: nothing extra
        log.warning("chat features: access check failed", exc_info=True)
        return {perm: False for perm in FEATURES}


def allowed(hub_dir: Path, email: str, feature: str) -> bool:
    return for_person(hub_dir, email).get(feature, False)
