"""Every secret a hub uses or expects, with where it comes from: the one
picture behind ``hubzoid secrets list``, the Console's Secrets tab and doctor.

Names, sources and status only. No row ever carries a value, a file path or an
AWS secret name (GitHub #46).

One row per name. ``level`` says where it lives: ``deployment``, ``hub`` or
``restricted``. ``status`` is one of:

    set          an operator source or the store provides a value
    missing      the hub expects it and nothing provides it
    blank        an operator source has the name with an empty value
    unreadable   stored, but this deployment key cannot decrypt it
    rebind       stored for a different destination (set it again)

A name a non-blank operator source sets is ``locked``: the store cannot change
it, and a stored value for it is kept but unused (``stored_status``
``overridden``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .. import config_secrets as cs
from ..assertions import hub_key
from . import layer as layer_lib
from . import names, store

SOURCE_LABELS = {
    cs.L_ENV: "Deployment environment",
    cs.L_DEPLOYMENT_SECRET: "Deployment secret",
    cs.L_HUB: "Hub .env",
    cs.L_HUB_SECRET: "Hub secret",
    cs.L_RESTRICTED: "Restricted file",
    cs.L_RESTRICTED_SECRET: "Restricted secret",
}
STORE_LABEL = "Stored"
_LOCK_REASONS = {
    cs.L_ENV: "Set in the deployment environment. Remove it there to manage it here.",
    cs.L_DEPLOYMENT_SECRET: "Set in the deployment's AWS secret. Remove it there to manage it here.",
    cs.L_HUB: "Set in the hub .env file. Remove the line there to manage it here.",
    cs.L_HUB_SECRET: "Set in the hub's AWS secret. Remove it there to manage it here.",
    cs.L_RESTRICTED: "Set in restricted/.env. Remove the line there to manage it here.",
    cs.L_RESTRICTED_SECRET: "Set in the restricted AWS secret. Remove it there to manage it here.",
}
_RESTRICTED_LAYERS = {cs.L_RESTRICTED, cs.L_RESTRICTED_SECRET}
_DEPLOYMENT_LAYERS = {cs.L_ENV, cs.L_DEPLOYMENT_SECRET}
_LEVEL_ORDER = {"deployment": 0, "hub": 1, "restricted": 2}
_STATUS_FOR_STORED = {layer_lib.IN_USE: "set", layer_lib.UNREADABLE: "unreadable",
                      layer_lib.REBIND: "rebind"}


def _example_names(path: Path) -> list[str]:
    return [k for k in cs._read_file(path) if names.env_name(k)]


def expected(hub_dir: Path, env: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """{name: (level, why)} for the secrets this hub needs before anything
    sets them: its model's key, its webhooks' keys, the secret settings in its
    settings.yaml, and the names in its ``.env.example`` files. Nothing is
    offered before something reads it."""
    out: dict[str, tuple[str, str]] = {}
    model = (env.get("MODEL") or "").strip()
    key = names.provider_key(model)
    if key:
        out[key] = ("hub", "model")
    try:
        from ..workflows.events import route_declarations

        for webhook in route_declarations(hub_dir):
            out.setdefault("WEBHOOK_SECRET_" + webhook.upper().replace("-", "_"), ("hub", "webhook"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..hubs.settings import declarations

        for decl in declarations(hub_dir):
            if decl.secret:
                out.setdefault(decl.name, ("hub", "setting"))
    except ValueError:  # an unreadable settings.yaml is doctor's to report
        pass
    for name in _example_names(hub_dir / ".env.example"):
        if names.secret_like(name):
            out.setdefault(name, ("hub", "declared"))
    for name in _example_names(hub_dir / "restricted" / ".env.example"):
        out[name] = ("restricted", "declared")
    return {k: v for k, v in out.items() if not names.operator_only(k)}


def rows(hub_dir: Path, *, env: Mapping[str, str] | None = None, fetch_secrets: bool = True,
         running: bool = False, strict: bool = False) -> list[dict]:
    """Every row for this hub, restricted ones included (``visible`` filters).

    ``env`` is the process environment before any hub layer (default: as this
    process started). ``running`` compares with what this bridge loaded at
    start, so a change since then reads as ``restart_needed``. ``strict``
    raises ``config_secrets.SecretFetchError`` when a named AWS secret cannot
    be read, instead of leaving its keys out (a write must know every owner)."""
    hub_dir = Path(hub_dir)
    base = cs._start_env() if env is None else dict(env)
    key = hub_key(hub_dir)

    # Operator sources, files and AWS secrets alike: the layer that sets each
    # name last, and whether its value is blank (a blank value sets nothing).
    operator: dict[str, tuple[str, bool]] = {}
    effective = dict(base)
    for layer_name, _source, values in cs._layers(hub_dir, base, fetch_secrets=fetch_secrets, strict=strict):
        for name, value in values.items():
            operator[name] = (layer_name, not (value or "").strip())
        effective.update(values)
    wanted = expected(hub_dir, effective)

    eng = store.existing_engine(hub_dir)
    stored = {("deployment", m.name): m for m in store.list_meta(hub_dir, scope="deployment", eng=eng)}
    stored.update({("hub", m.name): m for m in store.list_meta(hub_dir, scope="hub", target=key, eng=eng)})
    readable: dict[tuple[str, str], bool] = {}
    if eng is not None:
        for scope, target in (("deployment", ""), ("hub", key)):
            for item in store.values(hub_dir, scope=scope, target=target, eng=eng):
                readable[(scope, item.meta.name)] = item.value is not None
    at_start = layer_lib.started(hub_dir) if running else {}

    # The process environment counts only for names the hub expects or stores,
    # so a developer's shell variables never show up in the Console.
    for name in set(wanted) | {n for _, n in stored}:
        if name not in operator and name in base:
            operator[name] = (cs.L_ENV, not (base.get(name) or "").strip())

    candidates = {n for n, (layer, _) in operator.items()
                  if layer in _RESTRICTED_LAYERS or names.secret_like(n) or n in wanted}
    candidates |= set(wanted) | {n for _, n in stored}
    if running:
        candidates |= {n for (_, n), (state, _) in at_start.items() if state == layer_lib.IN_USE}
    # Bootstrap and operator settings are never managed here (GitHub #49).
    candidates = {n for n in candidates if not (names.env_name(n) and names.operator_only(n))}

    out: list[dict] = []
    for name in candidates:
        op = operator.get(name)
        hub_meta, dep_meta = stored.get(("hub", name)), stored.get(("deployment", name))
        meta = hub_meta or dep_meta
        scope = "hub" if hub_meta else ("deployment" if dep_meta else None)
        if op and op[0] in _RESTRICTED_LAYERS or meta and meta.restricted:
            level = "restricted"
        elif op and op[0] in _DEPLOYMENT_LAYERS and not op[1]:
            level = "deployment"
        elif meta:
            level = "deployment" if scope == "deployment" else "hub"
        elif name in wanted:
            level = wanted[name][0]
        else:
            level = "hub"
        row = {"name": name, "level": level, "status": "missing", "source": None, "locked": False,
               "lock_reason": None, "stored": None, "stored_status": None,
               "expected": wanted[name][1] if name in wanted else None, "restart_needed": False}
        if op:
            row["source"] = SOURCE_LABELS.get(op[0], op[0])
            if op[1]:
                row["status"] = "blank"
            else:
                row.update(status="set", locked=True, lock_reason=_LOCK_REASONS.get(op[0]))
        if meta:
            row["stored"] = {**meta.public(), "scope": scope}
            if not readable.get((scope, name), True):
                state = layer_lib.UNREADABLE
            elif row["locked"]:
                state = layer_lib.OVERRIDDEN
            elif meta.destination is not None and names.destination(name, effective) != meta.destination:
                state = layer_lib.REBIND
            else:
                state = layer_lib.IN_USE
            row["stored_status"] = state
            if not row["locked"]:
                row["status"], row["source"] = _STATUS_FOR_STORED[state], STORE_LABEL
            if running:
                row["restart_needed"] = at_start.get((scope, name)) != (state, meta.revision)
        elif running:
            row["restart_needed"] = any(at_start.get((s, name), ("", 0))[0] == layer_lib.IN_USE
                                        for s in ("deployment", "hub"))
        out.append(row)
    return sorted(out, key=lambda r: (_LEVEL_ORDER[r["level"]], r["name"]))


def visible(all_rows: list[dict], *, restricted_names: bool) -> dict:
    """The rows a viewer may see. Without ``restricted_names`` the restricted
    rows become a count (an agent administrator's view)."""
    if restricted_names:
        return {"secrets": all_rows, "restricted_hidden": 0}
    shown = [r for r in all_rows if r["level"] != "restricted"]
    return {"secrets": shown, "restricted_hidden": len(all_rows) - len(shown)}
