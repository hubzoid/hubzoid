"""Slack attachment downloader.

`download_message_files` walks a `conversations.replies` history payload,
fetches every Slack-hosted file with the bot token, and forwards each
one to the bridge's `/uploads/{chat_id}/{filename}` route — so Slack
attachments land in the same per-chat uploads dir as data-URL uploads
and pick up the same size cap + sidecar metadata for free.

The function is the bridge between two boundaries (Slack API and the
bridge HTTP API) and keeps the adapter handlers small. Both seams are
injectable for tests (`slack_client`, `http`) so we don't talk to a
real Slack workspace in unit tests.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("hubzoid.slack.files")


def download_message_files(
    *,
    history: list[dict[str, Any]],
    slack_client: Any,
    http: httpx.Client,
    bridge_url: str,
    api_key: str,
    chat_id: str,
    bot_token: str,
    max_upload_bytes: int,
    already_seen: set[str] | None = None,
) -> dict[str, list[str]]:
    """Download every file in `history` and POST it to the bridge.

    Returns `{message_ts: [filenames_successfully_uploaded]}` so the
    caller can stitch attachment notes onto the matching user message.
    Failures (Slack info miss, oversized file, GET error, bridge 4xx) are
    logged and skipped — never raised, so a single bad file can't kill
    the turn.

    `already_seen` is an optional set of Slack file IDs the caller wants
    to skip on this call. The set is mutated in place with the IDs of
    every file successfully uploaded, so the caller can persist it
    across turns and dedupe re-fetches of the same attachment when
    Slack returns the full thread history on every message. Failed
    uploads are NOT added to the set — they'll retry next turn.
    """
    out: dict[str, list[str]] = {}
    for msg in history:
        if not isinstance(msg, dict):
            continue
        files = msg.get("files") or []
        if not isinstance(files, list):
            continue
        ts = msg.get("ts") or ""
        for f in files:
            if not isinstance(f, dict):
                continue
            file_id = f.get("id")
            if not file_id:
                continue
            if already_seen is not None and file_id in already_seen:
                continue
            try:
                info = (slack_client.files_info(file=file_id) or {}).get("file") or {}
            except Exception:  # noqa: BLE001
                log.exception("slack files_info failed for %s", file_id)
                continue
            name = info.get("name") or f.get("name") or file_id
            url = info.get("url_private_download")
            mimetype = info.get("mimetype") or "application/octet-stream"
            size = info.get("size") or 0
            if not url:
                log.warning("slack file %s has no url_private_download; skipping", file_id)
                continue
            if size and size > max_upload_bytes:
                log.warning(
                    "slack file %s (%s bytes) exceeds max_upload_bytes (%s); skipping",
                    name, size, max_upload_bytes,
                )
                continue
            try:
                resp = http.get(url, headers={"Authorization": f"Bearer {bot_token}"})
            except Exception:  # noqa: BLE001
                log.exception("slack file GET failed for %s", file_id)
                continue
            if resp.status_code != 200 or not resp.content:
                log.warning("slack file GET %s returned %s; skipping", file_id, resp.status_code)
                continue
            if len(resp.content) > max_upload_bytes:
                log.warning(
                    "slack file %s decoded to %s bytes > cap; skipping",
                    name, len(resp.content),
                )
                continue
            try:
                bridge_resp = http.post(
                    f"{bridge_url.rstrip('/v1').rstrip('/')}/uploads/{chat_id}/{name}",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": mimetype,
                    },
                    content=resp.content,
                )
            except Exception:  # noqa: BLE001
                log.exception("bridge POST /uploads failed for %s", name)
                continue
            if bridge_resp.status_code != 200:
                log.warning(
                    "bridge POST /uploads for %s returned %s; skipping",
                    name, bridge_resp.status_code,
                )
                continue
            out.setdefault(ts, []).append(name)
            if already_seen is not None:
                already_seen.add(file_id)
    return out
