"""Explicit, bounded Gmail history import through the ordinary email intake.

Preview is local and read-only. Applying its exact digest admits one page;
repeating the command resumes. Import never resets the new-mail cursor or
changes sender admission, quarantine, write modes or workflow authority.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import date, datetime, time as day_time, timezone

from .. import config, store
from ..errors import ChannelError
from . import poller, transports


def _scope(conn, query: str, after: str, before: str, limit: int) -> dict:
    if conn.provider != "email" or conn.settings.transport != "gmail":
        raise ChannelError("unsupported_transport", "History import requires a Gmail email connection.", status=422)
    query = str(query).strip()
    if not query or len(query) > 2000 or any(ord(c) < 32 for c in query):
        raise ChannelError("invalid_scope", "Provide a nonempty Gmail query of at most 2000 characters.", status=422)
    try:
        start, end = date.fromisoformat(after), date.fromisoformat(before)
    except (TypeError, ValueError):
        raise ChannelError("invalid_scope", "Use YYYY-MM-DD dates for --after and --before.", status=422) from None
    if start >= end or not 1 <= limit <= 10_000:
        raise ChannelError("invalid_scope", "The start must precede the end; limit must be 1 to 10000.", status=422)
    return {"connection": conn.id, "revision": conn.revision, "connection_digest": conn.digest,
            "mailbox": conn.settings.mailbox.lower(), "labels": sorted(conn.settings.gmail.label_ids),
            "query": query, "after": start.isoformat(), "before": end.isoformat(), "timezone": "UTC",
            "limit": limit}


def _digest(scope: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _epoch(value: str) -> int:
    return int(datetime.combine(date.fromisoformat(value), day_time(), tzinfo=timezone.utc).timestamp())


def summary(job: dict | None) -> dict | None:
    if not job:
        return None
    return {"state": job["state"], "digest": job["digest"], "scope": job["scope"],
            "processed": len(job.get("processed") or []), "pending": len(job.get("pending") or []),
            "deleted": job.get("deleted", 0), "outside_period": job.get("outside_period", 0),
            "updated_at": job.get("updated_at")}


def _checkpoint(conn, owner: str, job: dict, **extra) -> None:
    job["updated_at"] = time.time()

    def save(state):
        lease = state.get("email_poll_lease") or {}
        if lease.get("owner") != owner or float(lease.get("until") or 0) <= time.time():
            raise ChannelError("import_busy", "The mailbox lease changed. Repeat the same import to resume.", status=409)
        return {"email_import": dict(job), **extra}, None

    store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, save)


def _data(response, *, page_token: bool = False):
    if response.status_code >= 400:
        if response.status_code in (401, 403):
            raise ChannelError("auth_required", "Gmail refused access. Reconnect the configured mailbox.", status=403)
        if response.status_code == 429:
            raise ChannelError("rate_limited", "Gmail asked to slow down. Repeat the same import later.", status=503)
        if response.status_code == 400 and page_token:
            raise ChannelError("pagination_expired", "Gmail rejected the saved page token. Repeat the same approved "
                               "scope with --restart; completed message receipts are retained.", status=409)
        raise ChannelError("provider_error", f"Gmail could not complete the read ({response.status_code}). "
                           "Repeat the same import; its scope is unchanged.", status=502)
    return response.json()


def _get(transport, conn, path: str, **kwargs):
    return _data(transport.call(conn, "GET", path, **kwargs),
                 page_token=bool(kwargs.get("params", {}).get("pageToken")))


def import_history(conn, *, query: str, after: str, before: str, limit: int = 1000,
                   apply_digest: str | None = None, restart: bool = False, actor: str = "cli") -> dict:
    """Preview or process one page, returning only progress and intake receipts.

    This is an operator API, like ``poll_once``, not a model tool. Local CLI
    users own the host credentials. No inbox content is read on preview.
    """
    conn = config.active(conn.hub_dir, conn.id)
    scope = _scope(conn, query, after, before, limit)
    digest = _digest(scope)
    if apply_digest is None:
        if restart:
            raise ChannelError("approval_required", "--restart requires the exact --apply digest.", status=422)
        return {"state": "preview", "scope": scope, "digest": digest,
                "note": "No messages read. Applying admits matching mail through existing sender checks and "
                        "quarantine; it may start the configured workflow. Nothing bypasses write approvals."}
    if apply_digest != digest:
        raise ChannelError("scope_changed", "The digest does not match this mailbox, configuration, query, dates "
                           "and limit. Review a new preview before applying.", status=409)
    if not conn.enabled:
        raise ChannelError("connection_paused", "Enable this reviewed connection before importing.", status=409)
    owner = "history:" + secrets.token_hex(12)
    if not poller._lease(conn, owner, 120):
        return {"state": "busy", "digest": digest, "messages": 0}
    try:
        conn = config.active(conn.hub_dir, conn.id)
        if _digest(_scope(conn, query, after, before, limit)) != digest:
            raise ChannelError("scope_changed", "Connection changed. Review the import again.", status=409)
        job = dict((conn.state or {}).get("email_import") or {})
        if job.get("digest") != digest:
            job = {"digest": digest, "scope": scope, "state": "running", "pending": [],
                   "page_token": None, "listed": False, "processed": [], "deleted": 0, "outside_period": 0}
            config.audit(conn.hub_dir, actor, "email_history_started", conn.id)
        elif restart:
            # The query and all authority remain exactly the same. Existing
            # receipts and progress survive replay of expired provider pages.
            job.update(pending=[], page_token=None, listed=False, state="running")
            config.audit(conn.hub_dir, actor, "email_history_restarted", conn.id)
        elif job["state"] in ("complete", "limit_reached"):
            return {**summary(job), "messages": 0, "deliveries": []}
        transport = transports.Gmail()
        extra = {}
        if (conn.state or {}).get("email_cursor") is None:
            _, extra["email_cursor"] = transport.fetch_new(conn, None)
        _checkpoint(conn, owner, job, **extra)
        if not job["pending"]:
            params = {"q": f"({query.strip()}) after:{_epoch(after) - 1} before:{_epoch(before)}",
                      "labelIds": scope["labels"], "maxResults": transports.BATCH}
            if job["page_token"]:
                params["pageToken"] = job["page_token"]
            page = _get(transport, conn, "/gmail/v1/users/me/messages", params=params)
            job.update(pending=list(dict.fromkeys(str(m["id"]) for m in page.get("messages") or [])),
                       page_token=page.get("nextPageToken"), listed=True)
            _checkpoint(conn, owner, job)
        deliveries = []
        processed = set(job["processed"])
        while job["pending"] and len(processed) < limit:
            if not poller._lease(conn, owner, 120):
                raise ChannelError("import_busy", "Mailbox is being read elsewhere. Repeat to resume.", status=409)
            current = config.active(conn.hub_dir, conn.id)
            if not current.enabled or _digest(_scope(current, query, after, before, limit)) != digest:
                raise ChannelError("scope_changed", "Connection changed. Review the import again.", status=409)
            mid = job["pending"][0]
            if mid not in processed:
                response = transport.call(current, "GET", f"/gmail/v1/users/me/messages/{mid}",
                                          params={"format": "raw"})
                if response.status_code == 404:
                    job["deleted"] += 1
                else:
                    data = _data(response)
                    try:
                        timestamp = int(data["internalDate"])
                    except (KeyError, TypeError, ValueError):
                        raise ChannelError("provider_error", "Gmail omitted the message timestamp; import stopped "
                                           "without widening the approved period.", status=502) from None
                    if not _epoch(after) * 1000 <= timestamp < _epoch(before) * 1000:
                        job["outside_period"] += 1
                    else:
                        raw = transports._unb64url(data.get("raw") or "")
                        deliveries.append(poller.process(current, {"id": f"gmail:{mid}",
                            "raw": raw if len(raw) <= transports.MAX_RAW else b"",
                            "too_large": len(raw) > transports.MAX_RAW, "thread_id": data.get("threadId")}))
                processed.add(mid)
                job["processed"] = sorted(processed)
            job["pending"] = job["pending"][1:]
            _checkpoint(conn, owner, job)
        job["state"] = ("limit_reached" if len(processed) >= limit and (job["pending"] or job["page_token"])
                        else "complete" if job["listed"] and not job["pending"] and not job["page_token"]
                        else "running")
        _checkpoint(conn, owner, job)
        return {**summary(job), "messages": len(deliveries), "deliveries": deliveries}
    finally:
        poller._release(conn, owner)
