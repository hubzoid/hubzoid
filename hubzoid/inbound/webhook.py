"""Generic inbound webhook surface — the third plugin on the inbound harness.

WhatsApp and Telegram are chat surfaces: a person writes, the agent answers. A
lot of what an org actually wants to wire into a hub is not a person at all — an
alerting system (Squadcast, PagerDuty), a CI run, an Odoo automation, a form
backend. Those speak plain HTTP POST with a JSON body and a shared secret. This
surface receives them.

Unlike WhatsApp/Telegram there is no "sender" to resolve against the roster and
no reply to render, so this plugin does not touch the LLM. It does the one thing
every machine webhook needs and nothing more:

  * verify the request is authentic (shared secret, or HMAC-SHA256 of the body),
  * hand the payload to a **sink**.

The default sink writes the event to `<hub>/.inbound/webhooks/<name>/` as one
JSON file per delivery. A `schedule/*.md` task (or any hub tool) then reads that
inbox and decides what to do — notify a coordinator, open a ticket, page someone.
That keeps the surface unopinionated: hubzoid owns *receiving and proving*, the
hub owns *acting*. The sink is injectable so a hub can replace the default with
its own handler in tests or in code.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

log = logging.getLogger("hubzoid.inbound")

_SIG_PREFIX = "sha256="

# A webhook surface is on only when a secret is configured. Without a secret the
# endpoint would be an open, unauthenticated ingest — never serve it.
_WEBHOOK_VARS = ("WEBHOOK_INBOUND_SECRET",)


def missing_webhook_vars(env: Mapping[str, str]) -> "list[str]":
    return [v for v in _WEBHOOK_VARS if not (env.get(v) or "").strip()]


def _ct_equal(a: str, b: str) -> bool:
    """Constant-time compare that survives non-ASCII input (a raw
    ``compare_digest(str, str)`` raises on a byte > 0x7F; encoding fails closed)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _slug_segment(text: str) -> str:
    """A URL-safe path segment for the endpoint name (matches the hub-slug rule)."""
    out = "".join(c if c.isalnum() else "-" for c in str(text).strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


@dataclass
class WebhookConfig:
    """One generic webhook endpoint served at ``/webhooks/<hub>/<name>``.

    - ``secret``  the shared secret (required; the surface is off without it).
    - ``name``    the path segment, e.g. ``squadcast`` -> ``/webhooks/<hub>/squadcast``.
    - ``hmac``    when True, verify ``X-Signature-256: sha256=<hex>`` as
                  HMAC-SHA256(secret, raw body) — the style Squadcast/GitHub use.
                  When False, accept the secret verbatim via ``Authorization:
                  Bearer <secret>``, ``X-Webhook-Secret: <secret>``, or ``?token=``.
    - ``sink``    called with the parsed event dict on every verified delivery.
    """

    secret: str
    name: str = "webhook"
    hmac: bool = False
    sink: "Callable[[dict], None] | None" = None

    def authenticate(self, *, raw_body: bytes, headers: Mapping[str, str],
                     query: Mapping[str, str]) -> bool:
        """True iff this request proves it holds the secret. Fails closed."""
        if not self.secret:
            return False
        if self.hmac:
            header = headers.get("x-signature-256") or headers.get("X-Signature-256")
            if not header or not header.startswith(_SIG_PREFIX):
                return False
            provided = header[len(_SIG_PREFIX):]
            if not provided:
                return False
            expected = hmac.new(self.secret.encode("utf-8"), raw_body,
                                hashlib.sha256).hexdigest()
            return _ct_equal(provided, expected)
        # Shared-secret mode: three common carriers, any one is enough.
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            if _ct_equal(auth[7:].strip(), self.secret):
                return True
        supplied = (headers.get("x-webhook-secret") or headers.get("X-Webhook-Secret")
                    or query.get("token") or "")
        return bool(supplied) and _ct_equal(supplied, self.secret)


def webhook_config_from_env(env: Mapping[str, str], *, hub_dir: Path | None = None,
                            sink: "Callable[[dict], None] | None" = None
                            ) -> "WebhookConfig | None":
    """Build a WebhookConfig from env, or None when no secret is set.

    ``WEBHOOK_INBOUND_NAME`` (default ``webhook``) is the path segment,
    ``WEBHOOK_INBOUND_HMAC`` (truthy) switches to HMAC verification. With no
    explicit ``sink`` and a ``hub_dir``, the default file-inbox sink is used.
    """
    if missing_webhook_vars(env):
        return None
    name = _slug_segment(env.get("WEBHOOK_INBOUND_NAME") or "webhook") or "webhook"
    use_hmac = str(env.get("WEBHOOK_INBOUND_HMAC") or "").strip().lower() in (
        "1", "true", "yes", "on")
    if sink is None and hub_dir is not None:
        sink = make_file_sink(hub_dir, name)
    return WebhookConfig(secret=env["WEBHOOK_INBOUND_SECRET"].strip(), name=name,
                         hmac=use_hmac, sink=sink)


# Where verified events land, and where a schedule task's drained events are
# archived. Kept here so the receiver (this module) and the reader (the schedule
# trigger in `scheduling.py`) agree on the layout without importing each other's
# heavier siblings — this module is stdlib-only.
_PROCESSED_DIRNAME = ".processed"


def inbox_dir(hub_dir: Path, name: str) -> Path:
    """The directory holding pending events for the ``<name>`` webhook."""
    return Path(hub_dir) / ".inbound" / "webhooks" / name


def pending_events(hub_dir: Path, name: str) -> "list[Path]":
    """Every unprocessed event file for ``<name>``, oldest first.

    Only top-level ``*.json`` files count — archived events live under a
    ``.processed/`` subdir and are skipped, so a drained inbox reads as empty.
    The filename is ``<epoch_ns>-<rand>.json`` so lexical sort is time order.
    """
    d = inbox_dir(hub_dir, name)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if p.is_file())


def archive_events(paths: "list[Path]") -> None:
    """Move drained event files into the inbox's ``.processed/`` subdir.

    Called by the scheduler after a webhook-triggered task run SUCCEEDS, so the
    task is not re-fired for events it already handled. On failure the files are
    left in place and the task stays due — at-least-once delivery. Missing files
    (a task that deleted them itself) are ignored."""
    for p in paths:
        try:
            if not p.is_file():
                continue
            dest_dir = p.parent / _PROCESSED_DIRNAME
            dest_dir.mkdir(parents=True, exist_ok=True)
            p.replace(dest_dir / p.name)
        except OSError:
            log.exception("webhook: could not archive %s", p)


def make_file_sink(hub_dir: Path, name: str) -> "Callable[[dict], None]":
    """The default sink: append each event as a JSON file under the hub's inbox.

    ``<hub>/.inbound/webhooks/<name>/<epoch_ns>-<rand>.json`` — sortable by name,
    collision-free, and trivially readable by a schedule task or hub tool. The
    write is atomic (temp file + rename) so a reader (the schedule trigger) never
    sees a half-written event.
    """
    inbox = inbox_dir(hub_dir, name)

    def sink(event: dict) -> None:
        inbox.mkdir(parents=True, exist_ok=True)
        stamp = f"{time.time_ns()}-{os.urandom(4).hex()}"
        path = inbox / f"{stamp}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(event, ensure_ascii=False, indent=2))
        tmp.replace(path)  # atomic on POSIX — the reader sees all-or-nothing
        log.info("webhook: stored %s event -> %s", name, path.name)

    return sink
