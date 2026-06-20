"""Slack adapter — wires Slack events to HubZoid's OpenAI-compatible bridge.

Two paths, both backed by the same `stream_reply` pump:

  Assistant threads (sidebar)
      `assistant_thread_started` -> set_suggested_prompts (from AGENTS.md)
      `message` inside thread     -> set_status("Thinking...") + streamed reply

  Channel @mentions
      `app_mention` -> placeholder + throttled chat.update with the streamed reply

Both call `stream_reply`, which POSTs to `<bridge>/chat/completions` with
`stream=true` and yields content deltas. The adapter does not know which
runtime (claude-local vs LiteLLM) is on the other side — both speak the
same OpenAI-compatible SSE.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from slack_bolt import App, Assistant
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .. import settings as settingslib
from .conversion import (
    messages_from_thread,
    parse_sse_delta,
    to_slack_mrkdwn,
    truncate_for_slack,
    with_slack_format_hint,
)
from .env import validate_env
from .files import download_message_files


log = logging.getLogger("hubzoid.slack")


# Throttle chat.update to stay inside Slack's 1-per-second-per-channel cap
# without burning the budget. 0.75 s gives us headroom + a smoother UX.
_UPDATE_INTERVAL_S = 0.75


def _format_for_slack(text: str) -> str:
    """Convert standard markdown to Slack mrkdwn and apply the 40k cap.

    Used by every adapter path that posts agent text into Slack so the
    visual format and length safeguards stay consistent.
    """
    return truncate_for_slack(to_slack_mrkdwn(text))


# ---------------------------------------------------------------------------
# Streaming pump (pure-ish — easy to test by injecting an httpx mock)
# ---------------------------------------------------------------------------
def stream_reply(
    *,
    bridge_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    on_delta: Callable[[str], None],
    chat_id: str | None = None,
    http_client: httpx.Client | None = None,
    timeout: float | None = None,
) -> None:
    """POST to the bridge's /chat/completions with stream=true; forward content deltas.

    `bridge_url` should be the `/v1` base (e.g. `http://127.0.0.1:8000/v1`).
    `chat_id`, when provided, is forwarded in the body so the bridge
    scopes artifacts + uploads to the same Slack thread on every turn.
    Raises on HTTP error.
    """
    client = http_client or httpx.Client(timeout=timeout)
    owns_client = http_client is None
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    if chat_id:
        body["chat_id"] = chat_id
    try:
        with client.stream(
            "POST",
            f"{bridge_url}/chat/completions",
            # Declare the surface so the access guard refuses restricted tools
            # over Slack: it carries no per-person login, so a restricted door
            # is never reachable here. See hubzoid.access.policy.
            headers={"Authorization": f"Bearer {api_key}", "X-Hubzoid-Surface": "slack"},
            json=body,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                delta = parse_sse_delta(line)
                if delta:
                    on_delta(delta)
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# Throttled writer — buffers deltas, calls a writer fn every ~750ms
# ---------------------------------------------------------------------------
class _ThrottledWriter:
    """Accumulate text deltas; call writer with the cumulative string at most
    once per `interval` seconds. Always flushes on `done()`.

    Used for the channel `app_mention` path where we update one message via
    `chat.update`. (Inside Assistant threads we could use `set_status` for
    intermediate state, but the visible content path is the same shape.)
    """

    def __init__(self, writer: Callable[[str], None], interval: float = _UPDATE_INTERVAL_S):
        self._writer = writer
        self._interval = interval
        self._buf: list[str] = []
        self._lock = threading.Lock()
        self._last_flush = 0.0

    def feed(self, delta: str) -> None:
        should_flush = False
        text = ""
        with self._lock:
            self._buf.append(delta)
            now = time.monotonic()
            if now - self._last_flush >= self._interval:
                self._last_flush = now
                text = "".join(self._buf)
                should_flush = True
        # Call writer outside the lock so a slow Slack API doesn't block the
        # SSE iterator (would back-pressure the bridge).
        if should_flush:
            try:
                self._writer(text)
            except Exception:  # noqa: BLE001
                log.exception("slack chat.update failed (mid-stream, will retry on next tick)")

    def done(self) -> str:
        with self._lock:
            text = "".join(self._buf)
        if text:
            try:
                self._writer(text)
            except Exception:  # noqa: BLE001
                log.exception("slack chat.update failed (final flush)")
        return text


# ---------------------------------------------------------------------------
# build_app — wires Slack listeners to stream_reply
# ---------------------------------------------------------------------------
def build_app(
    *,
    hub_dir: Path,
    bridge_url: str,
    api_key: str,
    model_label: str,
    bot_token: str,
    suggestions: list[str] | None,
    bot_user_id: str | None = None,
    verify_token: bool = True,
    max_upload_bytes: int = settingslib.DEFAULT_MAX_UPLOAD_BYTES,
) -> App:
    """Construct a configured slack_bolt App. Does not start any sockets.

    `verify_token=False` skips slack-bolt's eager `auth.test` call against
    Slack — used by tests so they can construct an App with a fake token.
    Socket Mode doesn't use signing secrets, so we pass a placeholder.
    """
    app = App(
        token=bot_token,
        signing_secret="placeholder",  # Socket Mode doesn't need this
        raise_error_for_unhandled_request=False,
        token_verification_enabled=verify_token,
        request_verification_enabled=False,
    )
    assistant = Assistant()

    suggestions = list(suggestions or [])

    # Per-chat memo of every Slack file we've uploaded to the bridge.
    # Maps file_id -> (ts, filename). `conversations.replies` returns the
    # full thread on every turn, so without this we'd re-download every
    # attachment every message — wastes Slack rate budget AND re-pays
    # bridge ingest. We also re-emit the attachment notes for previously
    # seen files on every turn so the agent never loses track of what
    # was uploaded earlier in the thread.
    seen_files_by_chat: dict[str, dict[str, tuple[str, str]]] = {}

    def _gather_messages(client, context, channel: str, thread_ts: str) -> tuple[list[dict[str, str]], str]:
        """Common path: fetch history, download any Slack files, build messages.

        Returns (messages, chat_id). The chat_id is `slack-{channel}-{thread_ts}`
        and is sent to the bridge so artifacts/uploads scope to this thread.
        """
        history = client.conversations_replies(channel=channel, ts=thread_ts).get("messages") or []
        chat_id = f"slack-{channel}-{thread_ts}"
        bot_uid = bot_user_id or context.bot_user_id
        seen_map = seen_files_by_chat.setdefault(chat_id, {})
        already_seen_ids = set(seen_map.keys())
        with httpx.Client(timeout=30.0) as http:
            download_message_files(
                history=history,
                slack_client=client,
                http=http,
                bridge_url=bridge_url,
                api_key=api_key,
                chat_id=chat_id,
                bot_token=bot_token,
                max_upload_bytes=max_upload_bytes,
                already_seen=already_seen_ids,
            )
        # Walk this turn's history to backfill (file_id -> ts, filename) for
        # everything the download succeeded on. `already_seen_ids` was
        # mutated by download_message_files to include newly uploaded ids.
        for msg in history:
            if not isinstance(msg, dict):
                continue
            ts = msg.get("ts") or ""
            for f in (msg.get("files") or []):
                if not isinstance(f, dict):
                    continue
                fid = f.get("id")
                fname = f.get("name") or fid
                if fid and fid in already_seen_ids and fid not in seen_map:
                    seen_map[fid] = (ts, fname)
        # Rebuild full attached_files_by_ts from the complete memo so the
        # agent sees attachments on their original turn even after dedup
        # skips the re-download.
        attached: dict[str, list[str]] = {}
        for ts, fname in seen_map.values():
            attached.setdefault(ts, []).append(fname)
        msgs = messages_from_thread(
            history,
            bot_user_id=bot_uid,
            bot_id=context.bot_id,
            attached_files_by_ts=attached,
        )
        # Slack-only formatting guidance — kept out of Open WebUI / API
        # consumers which render markdown tables natively.
        msgs = with_slack_format_hint(msgs)
        return msgs, chat_id

    @assistant.thread_started
    def _on_thread_started(say, set_suggested_prompts, **_):
        say(f"Hi — I'm {hub_dir.name}. Ask me anything.")
        if suggestions:
            set_suggested_prompts(
                prompts=[{"title": s, "message": s} for s in suggestions[:4]],
                title="Try one of these:",
            )

    @assistant.user_message
    def _on_assistant_message(
        client,
        payload,
        context,
        set_status,
        say,
        **_,
    ):
        channel = payload["channel"]
        thread_ts = payload["thread_ts"]
        try:
            set_status("Thinking...")
            msgs, chat_id = _gather_messages(client, context, channel, thread_ts)
            if not msgs:
                say("(no question detected — try asking something specific)")
                return

            placeholder = say(text="…")
            ts = placeholder["ts"]

            def _update(text: str) -> None:
                client.chat_update(channel=channel, ts=ts, text=_format_for_slack(text) or "…")

            writer = _ThrottledWriter(_update)
            stream_reply(
                bridge_url=bridge_url,
                api_key=api_key,
                model=model_label,
                messages=msgs,
                on_delta=writer.feed,
                chat_id=chat_id,
            )
            final = writer.done()
            if not final:
                _update("(no response)")
        except Exception as exc:  # noqa: BLE001
            log.exception("assistant user_message handler failed")
            say(f":warning: error: {type(exc).__name__}: {exc}")

    app.assistant(assistant)

    @app.event("app_mention")
    def _on_mention(event, client, context, say):
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        try:
            msgs, chat_id = _gather_messages(client, context, channel, thread_ts)
            if not msgs:
                say(text="(empty mention)", thread_ts=thread_ts)
                return

            placeholder = say(text="…", thread_ts=thread_ts)
            ts = placeholder["ts"]

            def _update(text: str) -> None:
                client.chat_update(channel=channel, ts=ts, text=_format_for_slack(text) or "…")

            writer = _ThrottledWriter(_update)
            stream_reply(
                bridge_url=bridge_url,
                api_key=api_key,
                model=model_label,
                messages=msgs,
                on_delta=writer.feed,
                chat_id=chat_id,
            )
            final = writer.done()
            if not final:
                _update("(no response)")
        except Exception as exc:  # noqa: BLE001
            log.exception("app_mention handler failed")
            say(text=f":warning: error: {type(exc).__name__}: {exc}", thread_ts=thread_ts)

    # DM messages outside the AI sidebar (legacy IM path). The Assistant
    # middleware handles AI-thread DMs; this catches plain DMs that aren't
    # routed through assistant_thread_started.
    @app.event({"type": "message", "channel_type": "im"})
    def _on_im(event, client, context, say):
        # Skip bot's own messages (subtype="bot_message" or matching bot_id)
        if event.get("subtype") == "bot_message":
            return
        if context.bot_id and event.get("bot_id") == context.bot_id:
            return
        # Skip if this event is part of an assistant thread (assistant_thread
        # middleware already handled it).
        if event.get("assistant_thread"):
            return
        # Skip subtype message_changed/replies events.
        if event.get("subtype"):
            return

        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        try:
            msgs, chat_id = _gather_messages(client, context, channel, thread_ts)
            if not msgs:
                return
            placeholder = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="…")
            ts = placeholder["ts"]

            def _update(text: str) -> None:
                client.chat_update(channel=channel, ts=ts, text=_format_for_slack(text) or "…")

            writer = _ThrottledWriter(_update)
            stream_reply(
                bridge_url=bridge_url,
                api_key=api_key,
                model=model_label,
                messages=msgs,
                on_delta=writer.feed,
                chat_id=chat_id,
            )
            final = writer.done()
            if not final:
                _update("(no response)")
        except Exception as exc:  # noqa: BLE001
            log.exception("im handler failed")
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f":warning: error: {type(exc).__name__}: {exc}",
            )

    return app


# ---------------------------------------------------------------------------
# run — top-level entry (called from CLI)
# ---------------------------------------------------------------------------
def run(hub_dir: Path, *, env: dict[str, str] | None = None) -> int:
    """Load env, validate, build the app, and block on SocketModeHandler.

    Returns a CLI-friendly exit code. Logs a clear message before blocking so
    operators know the adapter is up.
    """
    from ..loaders import agents as agents_loader

    env = env if env is not None else os.environ
    validate_env(env)
    settings = settingslib.load(hub_dir)

    try:
        main = agents_loader.load_main(hub_dir)
        suggestions = list(main.spec.suggestions)
        agent_name = main.spec.name
    except Exception:  # noqa: BLE001
        suggestions = []
        agent_name = hub_dir.name

    model_label = settings.model_label or _slugify(agent_name)
    bridge_url = f"http://127.0.0.1:{settings.bridge_port}/v1"

    app = build_app(
        hub_dir=hub_dir,
        bridge_url=bridge_url,
        api_key=settings.first_api_key,
        model_label=model_label,
        bot_token=env["SLACK_BOT_TOKEN"],
        suggestions=suggestions,
        max_upload_bytes=settings.max_upload_bytes,
    )

    log.info("hubzoid slack adapter starting (hub=%s, bridge=%s)", hub_dir.name, bridge_url)
    handler = SocketModeHandler(app, env["SLACK_APP_TOKEN"])
    handler.start()
    return 0


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "agent"
