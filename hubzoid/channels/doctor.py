"""`hubzoid channels doctor`: read-only checks for one connection.

Nothing here posts a message, creates a provider subscription or changes a
setting. A network failure is never a green check, and a connection that has
not yet received a signed delivery says so: a working setup page is not proof
that the provider can reach the hub.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from . import config as config_lib
from . import store
from .config import Connection
from .errors import ChannelError
from .protocol import Check


def webhook_url(conn: Connection) -> str | None:
    base = (os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    if not base:
        return None
    return f"{base}/api/hubs/{conn.hub}/channels/{conn.id}/webhook"


def _public_url_check(conn: Connection) -> Check:
    base = (os.environ.get("HUBZOID_PUBLIC_URL") or "").strip()
    if not conn.adapter.webhook:
        return Check("public_url", "skip", "This transport connects out, so it needs no public URL.")
    if not base:
        return Check("public_url", "fail", "HUBZOID_PUBLIC_URL is not set, so the provider has nowhere to send "
                     "deliveries.", "public_url_required",
                     "Set HUBZOID_PUBLIC_URL to the deployment's HTTPS address, then run doctor again.")
    url = urlparse(base)
    if url.scheme == "https" and url.hostname:
        return Check("public_url", "ok", f"Deliveries go to {webhook_url(conn)}")
    if url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1"):
        return Check("public_url", "warn", f"{base} is local. Only a sender on this machine (or a tunnel you set "
                     "up) can reach it.", "public_url_required",
                     "For a real provider, set HUBZOID_PUBLIC_URL to an HTTPS address.")
    return Check("public_url", "fail", f"{base} is not HTTPS.", "public_url_unsafe",
                 "Providers sign deliveries, but the hub still needs HTTPS. Use an https:// address.")


def run(conn: Connection, inputs: dict | None = None) -> dict:
    inputs = dict(inputs or {})
    checks: list[Check] = []
    exported = config_lib.export(conn.hub_dir, conn.id)
    if exported["file_matches"] is False:
        checks.append(Check("file", "warn", f"{exported['file']} differs from the active revision "
                            f"{exported['revision']}.", "file_not_applied",
                            "Run hubzoid channels apply with the file to make it active, or export to undo the edit."))
    elif exported["file_matches"] is None:
        checks.append(Check("file", "warn", "The channel file is missing from the hub folder.", "file_missing",
                            "Export the active configuration back to channels/<id>.yaml."))
    else:
        checks.append(Check("file", "ok", f"The file matches active revision {conn.revision}."))
    checks.append(Check("enabled", "ok" if conn.enabled else "warn",
                        "Enabled: new requests start work." if conn.enabled else
                        "Paused: deliveries are recorded but start no work.",
                        "" if conn.enabled else "connection_disabled",
                        "" if conn.enabled else "Set enabled: true and apply when doctor passes."))
    v = config_lib.validate(conn.hub_dir, conn.config.model_dump())
    for e in v.errors:
        checks.append(Check(f"config:{e['field']}", "fail", e["message"], e["code"],
                            "Fix the file and apply it again."))
    for name in conn.adapter.secret_keys:
        ref = getattr(conn.settings, name, None)
        if not ref:
            continue
        try:
            conn.secret(name)
            checks.append(Check(f"secret:{name}", "ok", f"{ref} is set."))
        except ChannelError as exc:
            checks.append(Check(f"secret:{name}", "fail", exc.message, exc.code,
                                f"hubzoid secrets set {conn.hub_dir} --ref {ref} --stdin --apply"))
    checks.append(_public_url_check(conn))
    if conn.config.agent and not conn.config.allowed_groups:
        checks.append(Check("groups", "warn", "allowed_groups is empty, so nobody can start work from this "
                            "channel.", "no_groups", "List the groups whose members may ask, then apply."))
    links = store.list_links(conn.hub_dir, conn.hub, conn.provider)
    if not conn.adapter.customer_facing:     # outside senders are never linked to accounts
        checks.append(Check("links", "ok" if links else "warn",
                            f"{len(links)} linked {conn.provider} account(s)." if links else
                            "No provider accounts are linked to Hubzoid accounts yet.",
                            "" if links else "identity_unmapped",
                            "" if links else "Link each person with hubzoid channels link."))
    last = store.counts(conn.hub_dir, conn.hub, conn.id).get("last_delivery_at")
    checks.append(Check("delivery", "ok" if last else "warn",
                        "A verified delivery has arrived." if last else
                        "webhook_delivery: unverified. No signed delivery has arrived yet.",
                        "" if last else "delivery_unverified",
                        "" if last else "Send a real test request from the provider."))
    if inputs.get("actor"):
        checks.extend(_actor_checks(conn, str(inputs["actor"]), inputs.get("workspace_id")))
    try:
        checks.extend(conn.adapter.doctor(conn, inputs))
    except ChannelError as exc:
        checks.append(Check("provider", "fail", exc.message, exc.code, exc.extra.get("recovery", "")))
    except Exception as exc:  # noqa: BLE001 - a failed check is a fail, never a pass
        checks.append(Check("provider", "fail", f"The provider check could not run: {type(exc).__name__}.",
                            "provider_unavailable", "Check the network and credentials, then run doctor again."))
    return {"id": conn.id, "revision": conn.revision, "checks": [c.to_json() for c in checks],
            "ready": all(c.status != "fail" for c in checks) and conn.enabled,
            "webhook_url": webhook_url(conn) if conn.adapter.webhook else None, "network_writes": False}


def _actor_checks(conn: Connection, external_id: str, workspace_id: str | None) -> list[Check]:
    from .links import workspace_of

    out = []
    try:
        ws = workspace_of(conn, workspace_id)
    except ChannelError as exc:
        return [Check("actor", "fail", exc.message, exc.code)]
    link = store.resolve_link(conn.hub_dir, conn.hub, conn.provider, ws, external_id)
    if link is None:
        return [Check("actor:link", "fail", f"{external_id} is not linked to a Hubzoid account.",
                      "identity_unmapped", f"hubzoid channels link {conn.hub_dir} {conn.id} --external-id "
                                           f"{external_id} --account <email>")]
    out.append(Check("actor:link", "ok", f"{external_id} acts as {link['account_email']}."))
    from ..auth import users
    from .router import _groups_ok, _hub_access

    user = users.get(conn.hub_dir, link["account_id"])
    if user is None or (user.get("status") or "active") != "active":
        out.append(Check("actor:account", "fail", "That account is not active.", "account_inactive"))
        return out
    denied = _hub_access(conn.hub_dir, link["account_email"])
    out.append(Check("actor:hub", "ok" if not denied else "fail",
                     "Has access to this hub." if not denied else "No access to this hub.", denied or ""))
    if conn.config.agent or conn.config.allowed_groups:
        ok = _groups_ok(conn, link["account_email"])
        out.append(Check("actor:groups", "ok" if ok else "fail",
                         "In an allowed group." if ok else "Not in any of " + ", ".join(conn.config.allowed_groups
                                                                                        or ["(none)"]),
                         "" if ok else "group_not_allowed"))
    return out
