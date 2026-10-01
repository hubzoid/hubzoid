"""Conversation titles.

The first user message gives a provisional title at once (about 60 characters,
cut at a word). One direct, tool-free model call (``runtime.complete_once``,
never an agent run) then asks for a 3 to 6 word title in the background. Its
usage row is ``surface=web``, ``kind=background``. If the call fails or returns
nothing usable, the provisional title stays (``title_source`` 'fallback'). A
title the person set themselves (``title_source`` 'user') is never replaced.

The model is the hub's own, or ``HUBZOID_TITLE_MODEL`` when set. The call runs
on a small pool of daemon worker threads, so it never holds up a reply and
outlives the request that started it.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("hubzoid.chat.titles")

PROVISIONAL_MAX = 60
FALLBACK = "New conversation"
_PROMPT = ("Write a title of 3 to 6 words for a conversation that starts with the message "
           "below. Reply with the title only: no quotes, no trailing punctuation, no preamble."
           "\n\nMessage:\n{message}")
_SYSTEM = "You write short, plain titles for chat conversations."
_PREFIX = re.compile(r"^\s*(?:conversation\s+)?title\s*[:\-–]\s*", re.IGNORECASE)


def provisional(text: str) -> str:
    """The first message, on one line, cut at a word near 60 characters."""
    one = " ".join((text or "").split())
    if not one:
        return FALLBACK
    if len(one) <= PROVISIONAL_MAX:
        return one
    cut = one[:PROVISIONAL_MAX]
    if " " in cut[20:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-–") + "…"


def clean(raw: str | None) -> str | None:
    """A usable title from a model reply, or None."""
    lines = [line for line in (raw or "").strip().splitlines() if line.strip()]
    if not lines:
        return None
    text = _PREFIX.sub("", lines[0])
    text = text.strip(" \t\"'“”‘’`*#_").rstrip(" .!?;:,").strip(" \t\"'“”‘’`*#_")
    words = text.split()
    if not words:
        return None
    return " ".join(words[:8])[:80]


def generate(hub_dir: Path, conversation_id: str, message: str, subject: str | None) -> str | None:
    """One tool-free model call for a short title. None when it fails."""
    from .. import runtime as runtime_lib

    spec: dict = {"prompt": _PROMPT.format(message=(message or "")[:2000]), "system": _SYSTEM}
    model = (os.environ.get("HUBZOID_TITLE_MODEL") or "").strip()
    if model:
        spec["model"] = model
    try:
        out = runtime_lib.complete_once(hub_dir, spec, subject=subject, surface="web",
                                        kind="background", chat_id=conversation_id)
    except Exception as exc:  # noqa: BLE001 — a title is never worth an error
        log.warning("chat: title for %s not generated (%s)", conversation_id, type(exc).__name__)
        return None
    return clean(out.get("text"))


def settle(store, hub_dir: Path, conversation_id: str, message: str,
           subject: str | None) -> str | None:
    """Generate and store the title. Returns it when it replaced the
    provisional one."""
    title = generate(hub_dir, conversation_id, message, subject)
    if title and store.set_title(conversation_id, title, "auto", only_if_source=("pending",)):
        return title
    store.set_title_source(conversation_id, "fallback", only_if_source=("pending",))
    return None


class _Workers:
    """A few daemon threads for background title calls."""

    def __init__(self, size: int = 2):
        self._size = size
        self._jobs: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._pending = 0

    def submit(self, job: Callable[[], None]) -> None:
        with self._lock:
            self._pending += 1
            if len(self._threads) < self._size:
                thread = threading.Thread(target=self._work, name="hubzoid-titles", daemon=True)
                self._threads.append(thread)
                thread.start()
        self._jobs.put(job)

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job()
            except Exception:  # noqa: BLE001 — a title job must never kill its worker
                log.exception("chat: title job failed")
            finally:
                with self._lock:
                    self._pending -= 1
                    if self._pending == 0:
                        self._idle.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no job is pending (tests, orderly shutdown)."""
        with self._lock:
            return self._idle.wait_for(lambda: self._pending == 0, timeout=timeout)


workers = _Workers()


def start(store, hub_dir: Path, conversation_id: str, message: str, subject: str | None,
          on_title: Callable[[str], None] | None = None) -> None:
    """Queue the model title for a conversation; ``on_title`` hears the result."""

    def job() -> None:
        title = settle(store, hub_dir, conversation_id, message, subject)
        if title and on_title is not None:
            on_title(title)

    workers.submit(job)
