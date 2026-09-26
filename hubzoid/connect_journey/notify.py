"""Confirm connection outcomes back in WhatsApp.

Runs in the originating hub's inbound process, because only that process holds
the hub's WhatsApp credentials (any bridge may serve the link pages). A small
poller looks at this hub's WhatsApp journeys whose outcome has not been
handled, brings each up to date with its provider (so it also finishes a
journey whose person never reached the done page), and sends one message:

  * connected: "<App> is connected." plus, when the turn that asked is waiting,
    a one-use "Reply YES within 10 minutes to continue: <text>" offer.
  * started but failed or never finished: a one-time "not connected" note.
  * never opened, cancelled on the page, or replaced by a newer link: nothing.

Each outcome is claimed (`store.claim_notify`) before sending, so a message is
sent at most once even with several pollers. Before sending, the chat handle
must still resolve (through the hub roster) to the journey's subject.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from ..access.identity import normalize
from . import store
from .providers import label

log = logging.getLogger("hubzoid.connect")

INTERVAL_SECONDS = 5.0
_LOOKBACK_SECONDS = 86400
_PREVIEW_CHARS = 300


def _preview(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS - 1] + "…"


def message_for(j: dict) -> str | None:
    """The chat message for a finished journey, or None when it ends silently."""
    app = label(j["app"])
    if j["status"] == "connected":
        return f"{app} is connected. You can ask me to use it now."
    if j["status"] == "failed" or (j["status"] == "expired" and j.get("started")):
        return f"{app} was not connected. Ask me again for a new link when you are ready."
    return None


def tick(hub_dir, *, hub: str, send: Callable[..., object], resolver, surface: str = "whatsapp",
         gate=None, providers_for=None, now: float | None = None) -> int:
    """One pass of the outbox. Returns how many messages were sent.

    ``send(to=handle, text=...)`` delivers one chat message. ``resolver`` is
    the hub roster (``resolver(surface, handle) -> {"email", ...}``).
    ``providers_for(journey)`` overrides provider lookup (tests).
    """
    from . import finalize

    now = time.time() if now is None else now
    sent = 0
    for j in store.outbox(hub_dir, hub=hub, surface=surface, since=now - _LOOKBACK_SECONDS):
        try:
            provider = providers_for(j) if providers_for is not None else None
            j = finalize(hub_dir, j, provider=provider, gate=gate, now=now)
            if j["status"] in store.OPEN:
                continue
            text = message_for(j)
            if not store.claim_notify(hub_dir, j["id"], now=now):
                continue  # another poller handled it
            if text is None:
                continue
            handle = j.get("handle")
            who = resolver(surface, handle) if (resolver and handle) else None
            if not who or normalize(who.get("email") or "") != j["subject"]:
                log.warning("connect: %s outcome not sent; the chat no longer maps to the "
                            "account that asked", j["app"])
                continue
            if j["status"] == "connected":
                offer = store.offer_continuation(hub_dir, j["id"])
                if offer:
                    text += (f"\n\nReply YES within {store.OFFER_SECONDS // 60} minutes to "
                             f"continue: {_preview(offer)}")
            try:
                send(to=handle, text=text)
                sent += 1
            except Exception:  # noqa: BLE001 — at most once: a failed send is not retried
                log.warning("connect: could not send the %s outcome to the chat", j["app"],
                            exc_info=True)
        except Exception:  # noqa: BLE001 — one bad row never stops the outbox
            log.exception("connect: outbox failed for one journey")
    try:
        store.expire_offers(hub_dir, hub=hub, now=now)
    except Exception:  # noqa: BLE001
        log.debug("connect: could not expire continuation offers", exc_info=True)
    return sent


class Poller:
    """Runs :func:`tick` every few seconds on a daemon thread."""

    def __init__(self, hub_dir, *, send, resolver, surface: str = "whatsapp",
                 interval: float = INTERVAL_SECONDS, gate=None):
        self.hub_dir = Path(hub_dir)
        self.hub = normalize(self.hub_dir.resolve().name)
        self._send = send
        self._resolver = resolver
        self._surface = surface
        self._interval = interval
        self._gate = gate
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hubzoid-connect-outbox",
                                        daemon=True)
        self._thread.start()
        log.info("connect: WhatsApp confirmation poller started for %s", self.hub)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                tick(self.hub_dir, hub=self.hub, send=self._send, resolver=self._resolver,
                     surface=self._surface, gate=self._gate)
            except Exception:  # noqa: BLE001
                log.exception("connect: outbox pass failed")
