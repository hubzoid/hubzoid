"""Sandboxes: allocate, attach, stop, release and clean up.

Every sandbox has one owner (a station call, or a probe) and a row in
`hz_sandboxes`, written before the provider is called, so a restart finds it
again and a crash leaves nothing unaccounted for. The lifecycle:

1. allocate: the row, then the container, then any repository checkout;
2. setup: the profile's reviewed command, once, with only the setup network;
3. offline: every setup process ends, all network is removed and checked;
4. ready: agent tools run, always through `attach`;
5. release: the provider resources are deleted once. Hub work, outputs and
   history are never touched.

`attach` never recreates a sandbox. One that disappeared is marked `lost` and
the caller gets `sandbox_lost`. A stopped one is started again with its files.
"""
from __future__ import annotations

import hashlib
import re
import json
import logging
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy import text

from .. import redaction
from ..work_items import store as wi_store
from ..work_items.errors import WorkItemError, not_found, revision_conflict
from . import config as config_lib

log = logging.getLogger(__name__)

LIVE = ("allocating", "setting_up", "ready", "stopped")
TERMINAL = ("deleted", "lost", "start_failed", "setup_failed")
_JSON = ("profile_json", "provider_json", "network_json", "setup_json", "checkouts_json", "exports_json")
_PROVIDERS: dict = {}


# -- rows ----------------------------------------------------------------------------

def _engine(hub_dir):
    return wi_store.engine(hub_dir)


def _hub(hub_dir) -> str:
    return Path(hub_dir).name.lower()


def deployment_id(hub_dir) -> str:
    """Labels every provider resource, so cleanup never touches another
    deployment's sandboxes."""
    return "hz" + hashlib.sha256(str(Path(hub_dir).resolve()).encode()).hexdigest()[:12]


def _decode(row):
    if row is None:
        return None
    out = dict(row)
    for col in _JSON:
        if out.get(col) is not None:
            out[col] = json.loads(out[col])
    return out


def get(hub_dir, sandbox_id: str) -> dict | None:
    with _engine(hub_dir).connect() as c:
        return _decode(c.execute(text("SELECT * FROM hz_sandboxes WHERE id=:i AND hub=:h"),
                                 {"i": sandbox_id, "h": _hub(hub_dir)}).mappings().first())


def require(hub_dir, sandbox_id: str) -> dict:
    row = get(hub_dir, sandbox_id)
    if row is None:
        raise not_found("sandbox")
    return row


def list_rows(hub_dir, *, state: str | None = None, owner_kind: str | None = None,
              owner_id: str | None = None, live: bool | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM hz_sandboxes WHERE hub=:h"
    args: dict = {"h": _hub(hub_dir), "n": max(1, min(limit, 500))}
    if state:
        sql += " AND state=:s"
        args["s"] = state
    if owner_kind:
        sql += " AND owner_kind=:k"
        args["k"] = owner_kind
    if owner_id:
        sql += " AND owner_id=:o"
        args["o"] = owner_id
    if live is True:
        sql += " AND live_key IS NOT NULL"
    elif live is False:
        sql += " AND live_key IS NULL"
    with _engine(hub_dir).connect() as c:
        rows = c.execute(text(sql + " ORDER BY created DESC LIMIT :n"), args).mappings().all()
    return [_decode(r) for r in rows]


def _update(hub_dir, sandbox_id: str, *, expected_revision: int | None = None, **fields) -> dict:
    fields = {k: (wi_store.dumps(v) if k in _JSON and v is not None and not isinstance(v, str) else v)
              for k, v in fields.items()}
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    where = "id=:_id" + (" AND revision=:_rev" if expected_revision is not None else "")
    with _engine(hub_dir).begin() as c:
        changed = c.execute(text(f"UPDATE hz_sandboxes SET {sets}, revision=revision+1, updated=:_t WHERE {where}"),
                            dict(fields, _id=sandbox_id, _rev=expected_revision, _t=time.time())).rowcount
    row = get(hub_dir, sandbox_id)
    if not changed:
        raise revision_conflict(row["revision"] if row else 0)
    return row


def _end(hub_dir, sandbox_id: str, state: str, reason: str, *, error: str | None = None) -> dict:
    """Mark a sandbox finished. Its owner may then allocate another."""
    return _update(hub_dir, sandbox_id, state=state, end_reason=reason, live_key=None, ended_at=time.time(),
                   **({"error": error} if error else {}))


# -- providers ---------------------------------------------------------------------

def provider(binding: dict):
    from .docker_provider import DockerProvider

    key = tuple(sorted((k, str(v)) for k, v in binding.items()))
    if key not in _PROVIDERS:
        _PROVIDERS[key] = DockerProvider(binding)
    return _PROVIDERS[key]


def _provider_for(hub_dir, row: dict) -> object:
    """The provider a sandbox was created on: the binding pinned in its row,
    never whatever sandbox.yaml says now."""
    pinned = (row.get("provider_json") or {}).get("binding")
    if pinned:
        return provider(pinned)
    deployment = config_lib.load_deployment(hub_dir)
    name = row.get("provider_binding")
    if name not in deployment.providers:
        raise WorkItemError("provider_missing", f"Provider {name!r} is no longer in sandbox.yaml.", status=503)
    return provider(deployment.providers[name])


def _summary(exc: BaseException) -> str:
    """What a sandbox record keeps about a failure: the code and its first
    line, never command output."""
    code = getattr(exc, "code", None) or type(exc).__name__
    first = (getattr(exc, "message", None) or str(exc)).split("\n", 1)[0]
    return redaction.scrub_text(f"{code}: {first}")[:500]


# -- lifecycle -----------------------------------------------------------------------

def allocate(hub_dir, *, owner_kind: str, owner_id: str, profile_id: str, item_id: str | None = None,
             actor: str = "", checkout: dict | None = None, profile_json: dict | None = None) -> dict:
    """The owner's live sandbox, made ready. An existing live sandbox is
    attached (never replaced); a lost one raises `sandbox_lost`."""
    existing = list_rows(hub_dir, owner_kind=owner_kind, owner_id=owner_id, live=True, limit=1)
    if existing and existing[0]["state"] in ("allocating", "setting_up"):
        # Its owner runs one attempt at a time, so this allocation was cut off
        # (the process stopped). No agent tool ran in it: clean it up and start
        # again.
        _discard_interrupted(hub_dir, existing[0])
    elif existing:
        return attach(hub_dir, existing[0]["id"])
    deployment = config_lib.load_deployment(hub_dir)
    profile = (config_lib.Profile(**profile_json) if profile_json is not None
               else config_lib.load_profile(hub_dir, profile_id))
    config_lib.check_profile(hub_dir, profile, deployment)
    binding_name, binding = deployment.provider(profile.provider)
    sandbox_id = str(uuid.uuid4())
    now = time.time()
    from sqlalchemy.exc import IntegrityError

    with wi_store.locked(hub_dir, f"sandbox-allocate:{_hub(hub_dir)}") as conn:
        live = conn.execute(text("SELECT count(*) FROM hz_sandboxes WHERE hub=:h AND live_key IS NOT NULL"),
                            {"h": _hub(hub_dir)}).scalar()
        if live >= deployment.max_active_per_hub:
            raise WorkItemError("capacity_exceeded", f"This hub already has {live} active sandboxes, its limit. "
                                "Wait, stop idle sandboxes or raise limits.max_active_per_hub.", status=429)
        try:
            conn.execute(text(
                "INSERT INTO hz_sandboxes (id, hub, deployment, owner_kind, owner_id, item_id, live_key, profile_id, "
                "profile_digest, profile_json, provider_binding, provider_kind, state, network_json, created_by, "
                "created, lifetime_deadline, revision, updated) VALUES (:id, :h, :d, :ok, :oi, :it, :lk, :p, :pd, "
                ":pj, :pb, 'docker', 'allocating', :nj, :by, :t, :ld, 1, :t)"),
                {"id": sandbox_id, "h": _hub(hub_dir), "d": deployment_id(hub_dir), "ok": owner_kind,
                 "oi": owner_id, "it": item_id, "lk": f"{owner_kind}:{owner_id}", "p": profile.id,
                 "pd": profile.digest, "pj": wi_store.dumps(profile.to_json()), "pb": binding_name,
                 "nj": wi_store.dumps({"setup": dict(profile.network["setup"], hosts=profile.setup_hosts()),
                                       "agent": profile.network["agent"]}),
                 "by": actor or "", "t": now, "ld": now + profile.lifecycle["max_lifetime_seconds"]})
        except IntegrityError:
            pass   # the owner allocated concurrently: attach that one below
    existing = list_rows(hub_dir, owner_kind=owner_kind, owner_id=owner_id, live=True, limit=1)
    if existing and existing[0]["id"] != sandbox_id:
        return attach(hub_dir, existing[0]["id"])
    return _bring_up(hub_dir, sandbox_id, profile, binding, checkout)


def _bring_up(hub_dir, sandbox_id: str, profile, binding: dict, checkout: dict | None) -> dict:
    prov = provider(binding)
    hosts = profile.setup_hosts() if profile.setup else []
    deployment = deployment_id(hub_dir)
    try:
        prov.prepare(profile, needs_proxy=bool(hosts))
        _update(hub_dir, sandbox_id, provider_json={"binding": dict(binding), "deployment": deployment})
        state = prov.create(sandbox_id, deployment, profile, with_setup_network=bool(hosts))
        row = _update(hub_dir, sandbox_id, state="setting_up", provider_json=state,
                      image=state.get("image") or "")
        checkouts = []
        if checkout:
            checkouts.append(_checkout(prov, state, checkout))
            row = _update(hub_dir, sandbox_id, checkouts_json=checkouts)
        setup = None
        if profile.setup:
            setup = prov.run_setup(sandbox_id, deployment, state, profile, hosts=hosts)
            setup["output"] = redaction.scrub_text(setup["output"])
            if setup["exit_code"] != 0:
                _update(hub_dir, sandbox_id, setup_json=setup)
                raise WorkItemError("setup_failed", f"The setup command exited with {setup['exit_code']}"
                                    + (" (timed out)" if setup.get("timed_out") else "")
                                    + ".\nLast output:\n" + setup["output"][-1500:], status=409,
                                    sandbox_id=sandbox_id)
        offline = prov.go_offline(state)
        if not offline["verified"]:
            raise WorkItemError("network_mode_unsupported", "The sandbox could not be confirmed offline after "
                                f"setup ({offline}). Nothing ran.", status=503, sandbox_id=sandbox_id)
        network = dict(row["network_json"], effective="deny_all", verified_at=time.time(),
                       setup_used=bool(hosts))
        return _update(hub_dir, sandbox_id, state="ready", ready_at=time.time(), last_activity=time.time(),
                       network_json=network, setup_json=setup)
    except BaseException as exc:
        code = getattr(exc, "code", None)
        state_name = "setup_failed" if code in ("setup_failed", "network_mode_unsupported") else "start_failed"
        try:
            prov.delete(sandbox_id, deployment)
        except Exception:  # noqa: BLE001 - the reaper retries by label
            log.warning("could not remove sandbox %s after a failed start", sandbox_id)
        _end(hub_dir, sandbox_id, state_name, "failed", error=_summary(exc))
        raise


def _discard_interrupted(hub_dir, row: dict) -> None:
    try:
        _provider_for(hub_dir, row).delete(row["id"], row["deployment"])
    except WorkItemError:
        log.warning("sandbox %s: provider unavailable, the reaper will remove it", row["id"])
    _end(hub_dir, row["id"], "setup_failed" if row["state"] == "setting_up" else "start_failed", "interrupted",
         error="interrupted: Hubzoid stopped while preparing this sandbox.")


def attach(hub_dir, sandbox_id: str) -> dict:
    """The sandbox, running and offline, ready for a tool call."""
    row = require(hub_dir, sandbox_id)
    if row["state"] in ("lost", "deleted"):
        raise _lost(row)            # gone for its owner, whoever removed it
    if row["state"] not in ("ready", "stopped"):
        raise WorkItemError("sandbox_unavailable", f"The sandbox is {row['state']}.", status=409,
                            sandbox_id=sandbox_id)
    if time.time() > row["lifetime_deadline"]:
        release(hub_dir, sandbox_id, "expired")
        raise WorkItemError("lifetime_exceeded", "The sandbox reached its maximum lifetime and was removed.",
                            status=409, sandbox_id=sandbox_id)
    prov = _provider_for(hub_dir, row)
    if prov.container(row["provider_json"], sandbox_id) is None:
        _end(hub_dir, sandbox_id, "lost", "missing", error="The container no longer exists.")
        raise _lost(row)
    prov.start(row["provider_json"])
    check = prov.verify_offline(row["provider_json"]) if row["state"] == "stopped" else None
    if check is not None and not check["verified"]:
        release(hub_dir, sandbox_id, "failed")
        raise WorkItemError("network_mode_unsupported", "The sandbox was no longer offline and was removed.",
                            status=503, sandbox_id=sandbox_id)
    if row["state"] == "ready":
        # Activity is not a change an operator must have seen: it does not
        # move the revision that stop and delete check.
        with _engine(hub_dir).begin() as c:
            c.execute(text("UPDATE hz_sandboxes SET last_activity=:t, updated=:t WHERE id=:i AND state='ready'"),
                      {"t": time.time(), "i": sandbox_id})
        return get(hub_dir, sandbox_id)
    return _update(hub_dir, sandbox_id, state="ready", last_activity=time.time(), stopped_at=None)


def _lost(row) -> WorkItemError:
    cause = ("An administrator removed the sandbox." if row.get("end_reason") == "operator" else
             "The sandbox was removed when its time ran out." if row.get("end_reason") == "expired" else
             "The sandbox disappeared.")
    return WorkItemError("sandbox_lost", f"{cause} It is not recreated empty: the work restarts from its last "
                         "saved point or stops for a person.", status=503, sandbox_id=row["id"])


def stop(hub_dir, sandbox_id: str, *, reason: str = "idle") -> dict:
    row = require(hub_dir, sandbox_id)
    if row["state"] != "ready":
        return row
    prov = _provider_for(hub_dir, row)
    if prov.container(row["provider_json"], sandbox_id) is None:
        return _end(hub_dir, sandbox_id, "lost", "missing", error="The container no longer exists.")
    prov.stop(row["provider_json"])
    return _update(hub_dir, sandbox_id, state="stopped", stopped_at=time.time(), end_reason=reason)


def release(hub_dir, sandbox_id: str, reason: str) -> dict:
    """Delete the sandbox's provider resources. Idempotent. Never touches the
    hub's own files, outputs or history."""
    row = require(hub_dir, sandbox_id)
    if row["state"] in TERMINAL:
        return row
    try:
        _provider_for(hub_dir, row).delete(sandbox_id, row["deployment"])
    except WorkItemError:
        log.warning("sandbox %s: provider unavailable, the reaper will remove it", sandbox_id)
    return _end(hub_dir, sandbox_id, "deleted", reason)


# -- operator actions ----------------------------------------------------------------

def _require_operator(hub_dir, actor: str) -> None:
    from ..work_items import service as wi_service

    if actor.startswith("cli:") or wi_service._can(hub_dir, actor, "workflows_manage"):
        return
    raise WorkItemError("permission_denied", "Only a manager can stop or delete sandboxes.", status=403)


def operator_stop(hub_dir, sandbox_id, actor, *, reason: str, expected_revision: int | None = None) -> dict:
    _require_operator(hub_dir, actor)
    row = require(hub_dir, sandbox_id)
    if expected_revision is not None and expected_revision != row["revision"]:
        raise revision_conflict(row["revision"])
    _audit(hub_dir, actor, "sandbox_stop", sandbox_id, reason)
    return view(stop(hub_dir, sandbox_id, reason="operator"))


def operator_delete(hub_dir, sandbox_id, actor, *, reason: str, expected_revision: int | None = None) -> dict:
    _require_operator(hub_dir, actor)
    row = require(hub_dir, sandbox_id)
    if expected_revision is not None and expected_revision != row["revision"]:
        raise revision_conflict(row["revision"])
    _audit(hub_dir, actor, "sandbox_delete", sandbox_id, reason)
    return view(release(hub_dir, sandbox_id, "operator"))


def _audit(hub_dir, actor, action, subject, reason) -> None:
    from ..work_items import service as wi_service

    wi_service._audit(hub_dir, actor, action, f"{subject} ({(reason or '').strip()[:200]})", surface="sandbox")


def view(row: dict) -> dict:
    """Metadata only: never file contents or command output."""
    keys = ("id", "owner_kind", "owner_id", "item_id", "profile_id", "profile_digest", "provider_binding",
            "provider_kind", "image", "state", "end_reason", "created", "ready_at", "last_activity", "stopped_at",
            "ended_at", "lifetime_deadline", "revision", "created_by")
    out = {k: row.get(k) for k in keys}
    net = row.get("network_json") or {}
    out["network"] = {"setup": net.get("setup"), "agent": net.get("agent"), "effective": net.get("effective"),
                      "verified_at": net.get("verified_at")}
    profile = row.get("profile_json") or {}
    out["resources"] = profile.get("resources")
    out["idle_deadline"] = ((row.get("last_activity") or row["created"])
                            + (profile.get("lifecycle") or {}).get("idle_stop_seconds", 900)
                            if row["state"] == "ready" else None)
    setup = row.get("setup_json")
    out["setup"] = {"exit_code": setup.get("exit_code"), "timed_out": setup.get("timed_out")} if setup else None
    out["checkouts"] = [{k: c.get(k) for k in ("path", "commit", "ref", "source")}
                        for c in (row.get("checkouts_json") or [])]
    out["exports"] = row.get("exports_json") or []
    out["error"] = row.get("error")
    return out


def list_view(hub_dir, *, state=None, owner_kind=None, limit=50) -> dict:
    rows = list_rows(hub_dir, state=state, owner_kind=owner_kind, limit=limit)
    deployment = config_lib.load_deployment(hub_dir)
    return {"sandboxes": [view(r) for r in rows], "enabled": deployment.enabled,
            "providers": {n: {"kind": b["kind"], "runtime": b["runtime"]} for n, b in deployment.providers.items()},
            "limits": {"max_active_per_hub": deployment.max_active_per_hub},
            "last_reap": _LAST_REAP.get(_hub(hub_dir))}


# -- the reaper ------------------------------------------------------------------------

_LAST_REAP: dict = {}


def _owner_done(hub_dir, row) -> bool:
    """Whether the sandbox's owner has finished with it."""
    if row["owner_kind"] == "probe":
        return True
    if row["owner_kind"] == "station_call":
        from ..stations import service as station_service

        call = station_service.get_call(hub_dir, row["owner_id"])
        if call is None or call["status"] in ("completed", "cancelled"):
            return True
        if call["status"] == "failed":
            # Kept for the call's one retry while it is still possible.
            item = wi_store.get_item(hub_dir, call["item_id"])
            return bool(call["retry_used"]) or item is None or item["state"] in ("completed", "cancelled")
    return False


def reap(hub_dir, *, dry_run: bool = True) -> dict:
    """Stop idle sandboxes, remove expired ones, interrupted ones and those
    whose owner is done, and delete leftover provider resources labelled for
    this deployment that have no live record. With `dry_run` nothing
    changes; the plan is returned."""
    deployment = config_lib.load_deployment(hub_dir)
    now = time.time()
    plan = {"stop": [], "release": [], "interrupted": [], "orphans": []}
    live_rows = list_rows(hub_dir, live=True, limit=500)
    for row in live_rows:
        life = (row.get("profile_json") or {}).get("lifecycle") or {}
        setup_limit = ((row.get("profile_json") or {}).get("setup") or {}).get("timeout_seconds", 900)
        if row["state"] in ("allocating", "setting_up"):
            if now - row["updated"] > setup_limit + 600:
                plan["interrupted"].append({"id": row["id"], "reason": "interrupted"})
        elif now > row["lifetime_deadline"]:
            plan["release"].append({"id": row["id"], "reason": "expired"})
        elif _owner_done(hub_dir, row) and now >= (row.get("last_activity") or row["created"]) + deployment.terminal_seconds:
            plan["release"].append({"id": row["id"], "reason": "completed"})
        elif row["state"] == "ready" and now > (row.get("last_activity") or row["created"]) + life.get(
                "idle_stop_seconds", 900):
            plan["stop"].append({"id": row["id"], "reason": "idle"})
    live_ids = {r["id"] for r in live_rows}
    bindings = {tuple(sorted((k, str(v)) for k, v in b.items())): b for b in deployment.providers.values()}
    for row in live_rows:
        pinned = (row.get("provider_json") or {}).get("binding")
        if pinned:
            bindings[tuple(sorted((k, str(v)) for k, v in pinned.items()))] = pinned
    providers, seen_endpoints = [], set()
    for binding in bindings.values():
        endpoint = binding.get("endpoint") or "default"
        if endpoint in seen_endpoints:
            continue                # one engine is listed once
        seen_endpoints.add(endpoint)
        try:
            providers.append(provider(binding))
        except WorkItemError:
            continue
    seen = set()
    for prov in providers:
        for res in prov.resources(deployment_id(hub_dir)):
            sid = res.get("sandbox_id")
            if sid in live_ids or sid in seen:
                continue
            if res.get("created") is None or now - res["created"] >= deployment.orphan_grace_seconds:
                plan["orphans"].append({"id": sid, "kind": res["kind"], "role": res["role"]})
                seen.add(sid)
    if not dry_run:
        for entry in plan["stop"]:
            stop(hub_dir, entry["id"], reason="idle")
        for entry in plan["release"]:
            release(hub_dir, entry["id"], entry["reason"])
        for entry in plan["interrupted"]:
            _discard_interrupted(hub_dir, get(hub_dir, entry["id"]))
        for entry in plan["orphans"]:
            for prov in providers:
                if entry["id"]:
                    prov.delete(entry["id"], deployment_id(hub_dir))
    result = {"dry_run": dry_run, "at": now, **plan}
    if not dry_run:
        _LAST_REAP[_hub(hub_dir)] = {"at": now, "orphans": len(plan["orphans"]),
                                     "released": len(plan["release"]) + len(plan["interrupted"]),
                                     "stopped": len(plan["stop"])}
    return result


async def reaper_loop(hub_dir) -> None:
    """Run the cleanup every `reaper.interval_seconds` while sandboxes are
    on. Started with the bridge; a quiet no-op otherwise."""
    import asyncio

    while True:
        interval = 300
        try:
            deployment = config_lib.load_deployment(hub_dir)
            interval = deployment.reaper_interval_seconds
            if deployment.enabled:
                await asyncio.to_thread(reap, hub_dir, dry_run=False)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - cleanup retries next time; it never stops the bridge
            log.warning("sandbox cleanup failed; it runs again in %ss", interval, exc_info=True)
        await asyncio.sleep(interval)


# -- probes and doctor -------------------------------------------------------------------

def probe(hub_dir, profile_id: str, argv: list[str], *, actor: str = "") -> dict:
    """Run one command in a fresh sandbox of `profile_id` with the agent
    network policy, then delete it."""
    if not argv:
        raise WorkItemError("invalid_request", "Give a command to run, after --.")
    deployment = config_lib.load_deployment(hub_dir)
    row = allocate(hub_dir, owner_kind="probe", owner_id=str(uuid.uuid4()), profile_id=profile_id, actor=actor)
    try:
        prov = _provider_for(hub_dir, row)
        result = prov.run(row["provider_json"], argv, timeout=deployment.max_probe_seconds)
        out = redaction.scrub_text(result["stdout"].decode("utf-8", errors="replace"))
        err = redaction.scrub_text(result["stderr"].decode("utf-8", errors="replace"))
        network = row["network_json"].get("effective")
        return {"sandbox_id": row["id"], "exit_code": result["exit_code"], "stdout": out[-50000:],
                "stderr": err[-50000:], "truncated": len(out) > 50000 or len(err) > 50000,
                "network": network, "setup": view(row)["setup"], "deleted": True}
    finally:
        release(hub_dir, row["id"], "completed")


def doctor(hub_dir) -> dict:
    """Is everything needed for sandboxes in place? Nothing is created."""
    checks = []

    def add(cid, ok, summary, recovery=None, status=None):
        checks.append({"id": cid, "status": status or ("pass" if ok else "fail"), "summary": summary,
                       "recovery_action": None if ok else recovery})

    try:
        deployment = config_lib.load_deployment(hub_dir)
    except WorkItemError as exc:
        add("config", False, exc.message, "Fix sandbox.yaml at the named line.")
        return {"valid": False, "checks": checks, "providers": {}, "profiles": {}}
    add("config", deployment.enabled, "Sandboxes are on." if deployment.enabled else
        "Sandboxes are off (no sandbox.yaml, or enabled: false).", "Add sandbox.yaml with enabled: true.")
    providers = {}
    for name, binding in deployment.providers.items():
        try:
            info = provider(binding).check()
            providers[name] = info
            add(f"provider:{name}", True, f"Docker {info['server_version']} reachable, runtime {info['runtime']}, "
                f"isolation {info['isolation']}, disk limit {info['disk_limit']}.")
            if binding["runtime"] not in info["runtimes"]:
                add(f"runtime:{name}", False, f"The engine has no {binding['runtime']} runtime.",
                    "Install it or use runtime: runc.")
        except WorkItemError as exc:
            providers[name] = {"reachable": False, "error": exc.message}
            add(f"provider:{name}", False, exc.message, "Start Docker, then run doctor again.")
    profiles = {}
    for pid in config_lib.list_profiles(hub_dir):
        try:
            profile = config_lib.load_profile(hub_dir, pid)
            config_lib.check_profile(hub_dir, profile, deployment)
            _, binding = deployment.provider(profile.provider)
            from .docker_provider import SQUID_IMAGE, image_name
            prov = provider(binding)
            prepared = prov.has_image(image_name(profile)) and (not profile.setup_hosts() or not profile.setup
                                                               or prov.has_image(SQUID_IMAGE))
            profiles[pid] = {"digest": profile.digest, "prepared": prepared, "image": image_name(profile),
                             "setup_hosts": len(profile.setup_hosts()) if profile.setup else 0,
                             "agent_network": "deny_all"}
            add(f"profile:{pid}", prepared, f"{profile.source} is valid"
                + ("" if prepared else ", but its image is not prepared") + ".",
                f"Run `hubzoid sandbox profiles prepare . {pid}`.", status=None if prepared else "warn")
        except WorkItemError as exc:
            profiles[pid] = {"error": exc.message}
            add(f"profile:{pid}", False, exc.message, "Correct the profile and validate again.")
    last = _LAST_REAP.get(_hub(hub_dir))
    if last:
        add("reaper", True, f"Last cleanup removed {last['orphans']} orphaned resources.")
    return {"valid": all(c["status"] != "fail" for c in checks), "checks": checks, "providers": providers,
            "profiles": profiles}


def prepare(hub_dir, profile_id: str) -> dict:
    deployment = config_lib.load_deployment(hub_dir)
    profile = config_lib.load_profile(hub_dir, profile_id)
    config_lib.check_profile(hub_dir, profile, deployment)
    _, binding = deployment.provider(profile.provider)
    out = provider(binding).prepare(profile, needs_proxy=bool(profile.setup and profile.setup_hosts()))
    return dict(out, profile=profile_id, digest=profile.digest)


# -- repository checkout and patch export ----------------------------------------------

def plain_url(url: str) -> str:
    """A repository URL without any user, password or token in it."""
    from urllib.parse import urlsplit, urlunsplit

    if "://" not in url:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _checkout(prov, state: dict, spec: dict) -> dict:
    """Clone a repository on the host, with the host's own Git credentials,
    and copy it into the sandbox without them: the remote is the plain URL,
    and no credential helper or header is copied."""
    url = spec.get("url") or spec.get("path")
    ref = spec.get("ref")
    if not isinstance(url, str) or not url:
        raise WorkItemError("invalid_request", "A checkout needs url (or a local path) and optionally ref.")
    clean = plain_url(url)
    with tempfile.TemporaryDirectory(prefix="hubzoid-checkout-") as tmp:
        dest = Path(tmp) / "repo"
        args = ["git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet", "--no-local"]
        if ref:
            args += ["--branch", ref]
        if spec.get("depth"):
            args += ["--depth", str(int(spec["depth"]))]
        result = subprocess.run(args + ["--", url, str(dest)], capture_output=True, text=True, timeout=600,
                                env=_git_env())
        if result.returncode != 0:
            raise WorkItemError("checkout_failed", "Could not clone the repository: "
                                + redaction.scrub_text(result.stderr)[-500:], status=409)
        pin = spec.get("commit")
        if pin:
            if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", pin):
                raise WorkItemError("invalid_request", "checkout commit is a commit SHA.")
            pinned = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(dest), "checkout", "--quiet",
                                     "--detach", pin], capture_output=True, text=True, timeout=120, env=_git_env())
            if pinned.returncode != 0:
                raise WorkItemError("checkout_failed", f"The commit {pin[:12]} is not in the repository's history.",
                                    status=409)
        _git(dest, "remote", "set-url", "origin", clean if "://" in clean else "https://invalid.local/repo.git")
        config = dest / ".git" / "config"
        lines = [line for line in config.read_text().splitlines()
                 if not any(k in line.lower() for k in ("extraheader", "credential", "askpass"))]
        config.write_text("\n".join(lines) + "\n")
        commit = _git(dest, "rev-parse", "HEAD").strip()
        target = "/workspace/" + (spec.get("dest") or "repo")
        copied = prov.write_tree(state, dest, target)
    return {"path": target, "source": clean, "ref": ref, "commit": commit, **copied}


def _git_env() -> dict:
    import os

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "SSH_AUTH_SOCK",
                                                           "GIT_SSH_COMMAND", "TMPDIR")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          timeout=60).stdout


def export_patches(hub_dir, sandbox_id: str, out_dir: Path) -> list[dict]:
    """Each checkout's new commits as a patch file in the hub's output, with
    its SHA-256. The sandbox gives bytes only; Git runs with hooks off."""
    row = attach(hub_dir, sandbox_id)
    prov = _provider_for(hub_dir, row)
    prov.quiesce(row["provider_json"])        # nothing the agent left running can change the commits now
    max_bytes = (row["profile_json"].get("transfer") or {}).get("max_export_bytes", 2 * 2**30)
    exports = []
    for co in row.get("checkouts_json") or []:
        argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", co["path"],
                "format-patch", "--stdout", f"{co['commit']}..HEAD"]
        res = prov.run(row["provider_json"], argv, timeout=120)
        if res["exit_code"] != 0:
            raise WorkItemError("export_failed", "git format-patch failed: "
                                + redaction.scrub_text(res["stderr"].decode(errors="replace"))[-500:], status=409)
        data = res["stdout"]
        status = prov.run(row["provider_json"], ["git", "-c", "core.fsmonitor=false", "-C", co["path"], "status",
                                                 "--porcelain", "--untracked-files=normal"], timeout=60)
        dirty = status["exit_code"] != 0 or bool(status["stdout"].strip())
        if len(data) > max_bytes:
            raise WorkItemError("file_too_large", "The patch is larger than the profile allows.", status=422)
        count = res["stdout"].count(b"\nSubject: ") + (1 if res["stdout"].startswith(b"Subject: ") else 0)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{Path(co['path']).name}-{co['commit'][:12]}.patch"
        target = out_dir / name
        import os
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        exports.append({"checkout": co["path"], "base": co["commit"], "commits": count, "bytes": len(data),
                        "dirty": dirty,
                        "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "file": str(target.relative_to(hub_dir)) if target.is_relative_to(hub_dir) else str(target)})
    _update(hub_dir, sandbox_id, exports_json=exports)
    return exports

