"""The shared inbound web app: one Starlette application serving the public
``/webhooks/<surface>`` routes for every configured surface.

Per message the flow is identical across surfaces: verify authenticity, drop
duplicates, resolve the sender against the roster (the allowlist), load the
recent conversation, dispatch to the bridge with the full array (Slack/OWUI
parity), render for the surface, send the reply, and record the exchange.
Unknown senders never reach the LLM. The heavy work runs in a background task so
we ack fast (Meta/Telegram redeliver on a slow ack; dedup absorbs the repeat).

`dispatch_fn` and each surface's `send_*` are injectable so the whole flow is
testable with a fake dispatch/send and no real HTTP.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from ..telegram import send as tg_send
from ..telegram.conversion import render_final as tg_render_final
from ..telegram.enrollment import enroll_contact, resolve_telegram
from ..telegram.parse import parse_update
from ..telegram.verify import verify_secret
from ..whatsapp import send as wa_send
from ..whatsapp.conversion import render_final as wa_render_final
from ..whatsapp.parse import parse_messages
from ..whatsapp.verify import verify_challenge, verify_signature
from .. import db
from .dedup import Dedup
from .dispatch import dispatch as default_dispatch
from .history import DEFAULT_MAX_MESSAGES, History
from .render import strip_thinking, strip_tool_calls

log = logging.getLogger("hubzoid.inbound")

# Fixed, LLM-free handshake / gate messages. General, enterprise-neutral,
# punctuation-clean. Overridable per hub via INBOUND_MSG_* env (see Messages).
DEFAULT_VERIFY_PROMPT = "Please tap the button below to verify your number."
DEFAULT_VERIFIED = "You are verified. How can I help?"
DEFAULT_NOT_REGISTERED = "This number is not registered for access."
DEFAULT_NOT_OWN_CONTACT = "Please share your own number to verify."
DEFAULT_PLEASE_VERIFY = "Please verify first. Tap the button below to share your number."

# Back-compat module constants (kept for anything importing them directly).
WA_NOT_REGISTERED = DEFAULT_NOT_REGISTERED
TG_VERIFY_PROMPT = DEFAULT_VERIFY_PROMPT
TG_VERIFIED = DEFAULT_VERIFIED
TG_NOT_REGISTERED = DEFAULT_NOT_REGISTERED
TG_NOT_OWN_CONTACT = DEFAULT_NOT_OWN_CONTACT
TG_PLEASE_VERIFY = DEFAULT_PLEASE_VERIFY

_CONTACT_KEYBOARD = {
    "keyboard": [[{"text": "Share my number", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

# Telegram edit-streaming: default minimum seconds between edits to the SAME
# message. Telegram rate-limits same-message edits (bursting past ~1/s risks
# 429s), so this is the practical floor; overridable per hub.
_STREAM_EDIT_INTERVAL = 1.0


@dataclass(frozen=True)
class Messages:
    """The handshake/gate strings, overridable per hub via env."""

    verify_prompt: str = DEFAULT_VERIFY_PROMPT
    verified: str = DEFAULT_VERIFIED
    not_registered: str = DEFAULT_NOT_REGISTERED
    not_own_contact: str = DEFAULT_NOT_OWN_CONTACT
    please_verify: str = DEFAULT_PLEASE_VERIFY

    @staticmethod
    def from_env(env) -> "Messages":
        """Override any message from INBOUND_MSG_* env; blank/unset keeps the default."""
        overrides = {
            "verify_prompt": (env.get("INBOUND_MSG_VERIFY_PROMPT") or "").strip(),
            "verified": (env.get("INBOUND_MSG_VERIFIED") or "").strip(),
            "not_registered": (env.get("INBOUND_MSG_NOT_REGISTERED") or "").strip(),
            "not_own_contact": (env.get("INBOUND_MSG_NOT_OWN_CONTACT") or "").strip(),
            "please_verify": (env.get("INBOUND_MSG_PLEASE_VERIFY") or "").strip(),
        }
        return Messages(**{k: v for k, v in overrides.items() if v})


@dataclass
class WhatsAppConfig:
    verify_token: str
    app_secret: str
    token: str
    phone_number_id: str
    send_text: Callable = wa_send.send_text
    mark_read: Callable = wa_send.mark_read


@dataclass
class TelegramConfig:
    secret_token: str
    bot_token: str
    bindings: object  # hubzoid.telegram.enrollment.Bindings
    send_message: Callable = tg_send.send_message
    send_chat_action: Callable = tg_send.send_chat_action
    edit_message_text: Callable = tg_send.edit_message_text
    stream: bool = False


def _clean_reply(reply: str) -> str:
    """The visible answer to store in history (reasoning + tool dropdowns stripped)."""
    visible, _ = strip_thinking(strip_tool_calls(reply or ""))
    return visible.strip()


def build_app(
    *, hub_dir, bridge_url, api_key, model, resolver,
    whatsapp: "WhatsAppConfig | None" = None,
    telegram: "TelegramConfig | None" = None,
    dispatch_fn: Callable = default_dispatch,
    messages: "Messages | None" = None,
    history_max: int = DEFAULT_MAX_MESSAGES,
    history_ttl_seconds: "float | None" = None,
    stream_interval: float = _STREAM_EDIT_INTERVAL,
) -> Starlette:
    inbound_dir = Path(hub_dir) / ".inbound"
    dedup = Dedup(inbound_dir / "dedup")
    history = History(db.engine_for(hub_dir), max_messages=history_max,
                      ttl_seconds=history_ttl_seconds)
    msgs = messages or Messages()
    routes = []

    if whatsapp is not None:
        routes += _whatsapp_routes(
            whatsapp, dedup, history, resolver, dispatch_fn, bridge_url, api_key, model, msgs,
        )
    if telegram is not None:
        routes += _telegram_routes(
            telegram, dedup, history, resolver, dispatch_fn, bridge_url, api_key, model, msgs,
            stream_interval,
        )
    return Starlette(routes=routes)


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------
def _whatsapp_routes(wa, dedup, history, resolver, dispatch_fn, bridge_url, api_key, model, msgs):
    async def get_handler(request):
        challenge = verify_challenge(dict(request.query_params), wa.verify_token)
        if challenge is None:
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(challenge)

    async def post_handler(request):
        raw = await request.body()
        if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), wa.app_secret):
            return PlainTextResponse("forbidden", status_code=403)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return PlainTextResponse("bad request", status_code=400)
        fresh = [m for m in parse_messages(payload) if dedup.claim(m.id)]
        return PlainTextResponse("ok", background=BackgroundTask(_process_wa, fresh))

    def _process_wa(messages):
        for m in messages:
            try:
                _handle_wa_message(m)
            except Exception:  # noqa: BLE001 — one bad message never sinks the batch
                log.exception("whatsapp: processing failed for %s", m.id)

    def _handle_wa_message(m):
        identity = resolver("whatsapp", m.handle) if resolver else None
        if not identity or not identity.get("email"):
            wa.send_text(phone_number_id=wa.phone_number_id, token=wa.token,
                         to=m.handle, text=msgs.not_registered)
            return
        # Blue ticks + typing indicator — the only in-progress cue WhatsApp offers
        # (no message editing, no presence). Lasts ~25s or until we send the reply.
        try:
            wa.mark_read(phone_number_id=wa.phone_number_id, token=wa.token,
                         message_id=m.id, typing=True)
        except Exception:  # noqa: BLE001
            log.debug("whatsapp: mark_read failed", exc_info=True)
        chat_id = f"whatsapp-{m.handle}"
        prior = history.load(chat_id)
        reply = dispatch_fn(
            bridge_url=bridge_url, api_key=api_key, model=model,
            messages=prior + [{"role": "user", "content": m.text}], surface="whatsapp",
            user_email=identity.get("email"), groups=identity.get("groups") or [],
            chat_id=chat_id,
        )
        text = wa_render_final(reply)
        if text.strip():
            wa.send_text(phone_number_id=wa.phone_number_id, token=wa.token,
                         to=m.handle, text=text)
        history.append(chat_id, "user", m.text)
        history.append(chat_id, "assistant", _clean_reply(reply))

    return [
        Route("/webhooks/whatsapp", get_handler, methods=["GET"]),
        Route("/webhooks/whatsapp", post_handler, methods=["POST"]),
    ]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def _telegram_routes(tg, dedup, history, resolver, dispatch_fn, bridge_url, api_key, model, msgs, stream_interval):
    async def post_handler(request):
        if not verify_secret(request.headers.get("X-Telegram-Bot-Api-Secret-Token"),
                             tg.secret_token):
            return PlainTextResponse("forbidden", status_code=403)
        try:
            update = await request.json()
        except Exception:  # noqa: BLE001
            return PlainTextResponse("bad request", status_code=400)
        uid = str(update.get("update_id")) if isinstance(update, dict) else None
        if not uid or not dedup.claim(uid):
            return PlainTextResponse("ok")  # malformed or duplicate -> ack, no work
        return PlainTextResponse("ok", background=BackgroundTask(_process_tg, update))

    def _process_tg(update):
        try:
            _handle_tg_update(update)
        except Exception:  # noqa: BLE001
            log.exception("telegram: processing failed")

    def _handle_tg_update(update):
        p = parse_update(update)
        if p is None:
            return
        if p.kind == "contact":
            res = enroll_contact(
                handle=p.handle, contact_user_id=p.contact_user_id,
                contact_phone=p.contact_phone, resolver=resolver, bindings=tg.bindings,
            )
            log.info("telegram enrollment: handle=%s shared_phone=%s -> %s",
                     p.handle, p.contact_phone, res.status)
            msg = {
                "verified": msgs.verified,
                "not_registered": msgs.not_registered,
                "not_own_contact": msgs.not_own_contact,
            }.get(res.status, msgs.not_registered)
            tg.send_message(token=tg.bot_token, chat_id=p.handle, text=msg)
            return
        if p.kind == "start":
            tg.send_message(token=tg.bot_token, chat_id=p.handle,
                            text=msgs.verify_prompt, reply_markup=_CONTACT_KEYBOARD)
            return
        if p.kind == "text":
            identity = resolve_telegram(p.handle, resolver, tg.bindings)
            if not identity or not identity.get("email"):
                tg.send_message(token=tg.bot_token, chat_id=p.handle,
                                text=msgs.please_verify, reply_markup=_CONTACT_KEYBOARD)
                return
            try:
                tg.send_chat_action(token=tg.bot_token, chat_id=p.handle, action="typing")
            except Exception:  # noqa: BLE001 — the indicator is best-effort
                log.debug("telegram: sendChatAction failed", exc_info=True)

            chat_id = f"telegram-{p.handle}"
            prior = history.load(chat_id)
            dispatch_kwargs = dict(
                bridge_url=bridge_url, api_key=api_key, model=model,
                messages=prior + [{"role": "user", "content": p.text}], surface="telegram",
                user_email=identity.get("email"), groups=identity.get("groups") or [],
                chat_id=chat_id,
            )
            if tg.stream:
                reply = _stream_reply(tg, p.handle, dispatch_fn, dispatch_kwargs)
            else:
                reply = dispatch_fn(**dispatch_kwargs)
                text = tg_render_final(reply)
                if text.strip():
                    tg.send_message(token=tg.bot_token, chat_id=p.handle, text=text)
            history.append(chat_id, "user", p.text)
            history.append(chat_id, "assistant", _clean_reply(reply))
            return
        # kind == "other" -> ignore

    def _stream_reply(tg, send_chat_id, dispatch_fn, dispatch_kwargs) -> str:
        """Send a placeholder, then edit it as tokens arrive (Telegram has no
        native token stream; editMessageText simulates character-by-character).
        Returns the full reply so the caller can record it in history."""
        placeholder = tg.send_message(token=tg.bot_token, chat_id=send_chat_id, text="…")
        message_id = ((placeholder or {}).get("result") or {}).get("message_id")
        # Re-assert typing right after the placeholder: sending a message clears
        # the indicator, so without this it lapses until the first edit.
        try:
            tg.send_chat_action(token=tg.bot_token, chat_id=send_chat_id, action="typing")
        except Exception:  # noqa: BLE001
            pass
        cum = [""]
        # Start the clock now, so a reply that finishes within the interval gets
        # just one final edit instead of an immediate placeholder-replace.
        state = {"last": time.monotonic(), "shown": ""}

        def on_delta(delta):
            cum[0] += delta
            now = time.monotonic()
            if now - state["last"] < stream_interval:
                return
            state["last"] = now
            # Keep the "typing…" indicator alive while we stream (it lasts ~5s).
            try:
                tg.send_chat_action(token=tg.bot_token, chat_id=send_chat_id, action="typing")
            except Exception:  # noqa: BLE001
                pass
            text = tg_render_final(cum[0]).strip()
            if text and text != state["shown"] and message_id is not None:
                state["shown"] = text
                try:
                    tg.edit_message_text(token=tg.bot_token, chat_id=send_chat_id,
                                         message_id=message_id, text=text)
                except Exception:  # noqa: BLE001 — a mid-stream edit failure is non-fatal
                    log.debug("telegram: mid-stream edit failed", exc_info=True)

        reply = dispatch_fn(on_delta=on_delta, **dispatch_kwargs)
        final = tg_render_final(reply).strip()
        if not final:
            # Nothing visible (e.g. the answer was entirely tool/think blocks).
            # Don't leave the "…" placeholder dangling.
            if message_id is not None:
                try:
                    tg.edit_message_text(token=tg.bot_token, chat_id=send_chat_id,
                                         message_id=message_id, text="(no response)")
                except Exception:  # noqa: BLE001
                    log.debug("telegram: placeholder cleanup failed", exc_info=True)
            return reply
        if message_id is None:
            tg.send_message(token=tg.bot_token, chat_id=send_chat_id, text=final)
        elif final != state["shown"]:
            try:
                tg.edit_message_text(token=tg.bot_token, chat_id=send_chat_id,
                                     message_id=message_id, text=final)
            except Exception:  # noqa: BLE001
                log.debug("telegram: final edit failed", exc_info=True)
        return reply

    return [Route("/webhooks/telegram", post_handler, methods=["POST"])]
