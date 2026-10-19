"""`channels/<id>.yaml`: load, validate, apply and export a connection.

The file is the desired configuration and lives in the hub folder, versioned
with the hub. The operational store holds the active, validated revision. A
file edit takes effect only through an explicit apply, and the Console and API
apply the same schema and rewrite the same file, so there is never a hidden
database-only setting.

Errors name the file, the key and its line (``hubzoid.control.yamlfile``).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from .. import _fs
from ..assertions import hub_key
from ..control import canonical
from ..control.yamlfile import YamlError
from ..control import yamlfile
from . import registry, store
from .errors import ChannelError, not_found
from .models import ChannelConfig

#: reference keys must end in _ref and hold secret: or env: references
_REF_KEY = re.compile(r".*_ref$")


def hub_of(hub_dir) -> str:
    return hub_key(Path(hub_dir))


def channels_dir(hub_dir, *, create: bool = False) -> Path:
    found = _fs.resolve_bucket(Path(hub_dir), "channels")
    if found is not None:
        return found
    path = Path(hub_dir) / "channels"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def file_for(hub_dir, cid: str) -> Path:
    folder = channels_dir(hub_dir)
    for ext in (".yaml", ".yml"):
        p = folder / f"{cid}{ext}"
        if p.is_file():
            return p
    return folder / f"{cid}.yaml"


def files(hub_dir) -> list[Path]:
    folder = channels_dir(hub_dir)
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file())


# -- validation -------------------------------------------------------------------

@dataclass
class Validation:
    valid: bool
    errors: list[dict] = field(default_factory=list)
    config: ChannelConfig | None = None
    settings: BaseModel | None = None
    data: dict | None = None
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        out: dict[str, Any] = {"valid": self.valid, "errors": self.errors, "warnings": self.warnings,
                               "network_writes": False}
        if self.config is not None:
            adapter = registry.get(self.config.provider)
            out["normalized"] = normalized(self.config, self.settings)
            out["capabilities"] = {"provider": adapter.provider, "title": adapter.title or adapter.provider,
                                   "webhook": adapter.webhook, "secret_keys": list(adapter.secret_keys),
                                   "reply_part_chars": adapter.part_chars, "max_parts": adapter.max_parts,
                                   "uncertain_post": adapter.recovery}
        return out


def _pointer(loc) -> str:
    return "/" + "/".join(str(p) for p in loc)


def _err(code: str, field_path: str, message: str, lines: dict | None = None) -> dict:
    line = None
    if lines:
        probe = field_path
        while probe and probe not in lines:
            probe = probe.rsplit("/", 1)[0]
        line = lines.get(probe or "")
    return {"code": code, "field": field_path, "line": line, "message": message}


def normalized(cfg: ChannelConfig, settings: BaseModel | None) -> dict:
    data = cfg.model_dump()
    if settings is not None:
        data["provider_config"] = settings.model_dump(mode="json")
    return data


def validate(hub_dir, data: dict, *, lines: dict | None = None, path: str | None = None,
             expected_id: str | None = None) -> Validation:
    """Check the file shape, the provider's own settings and every reference.
    Makes no network call and changes nothing."""
    hub_dir = Path(hub_dir)
    errors: list[dict] = []
    if not isinstance(data, dict):
        return Validation(False, [_err("invalid_config", "/", "The file must be a mapping.")])
    try:
        cfg = ChannelConfig.model_validate(data)
    except ValidationError as exc:
        for e in exc.errors():
            loc = [p for p in e.get("loc", ()) if p != "__root__"]
            msg = e.get("msg", "invalid").removeprefix("Value error, ")
            if e.get("type") == "extra_forbidden":
                msg = f"{loc[-1]!r} is not a channel setting. Provider settings go under provider_config."
            errors.append(_err("invalid_config", _pointer(loc), msg, lines))
        return Validation(False, errors, data=data)
    if expected_id is not None and cfg.id != expected_id:
        errors.append(_err("invalid_config", "/id", f"The file's id is {cfg.id!r}, but this is "
                                                     f"{expected_id!r}. The id never changes.", lines))
    try:
        adapter = registry.get(cfg.provider)
    except ChannelError as exc:
        errors.append(_err(exc.code, "/provider", exc.message, lines))
        return Validation(False, errors, config=cfg, data=data)
    settings = None
    try:
        settings = adapter.validate(cfg.provider_config, hub_dir=hub_dir, connection_id=cfg.id)
    except ValidationError as exc:
        for e in exc.errors():
            loc = ["provider_config", *[p for p in e.get("loc", ()) if p != "__root__"]]
            msg = e.get("msg", "invalid").removeprefix("Value error, ")
            if e.get("type") == "extra_forbidden":
                msg = f"{loc[-1]!r} is not a {cfg.provider} setting."
            errors.append(_err("invalid_config", _pointer(loc), msg, lines))
    except ChannelError as exc:
        errors.append(_err(exc.code, exc.extra.get("field", "/provider_config"), exc.message, lines))
    errors.extend(_check_refs(hub_dir, cfg, lines))
    errors.extend(_check_target(hub_dir, cfg, lines))
    return Validation(not errors, errors, config=cfg, settings=settings, data=data)


def _walk_refs(value, path: str):
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}/{k}"
            if isinstance(k, str) and _REF_KEY.match(k) and v is not None:
                yield p, v
            else:
                yield from _walk_refs(v, p)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_refs(v, f"{path}/{i}")


def _check_refs(hub_dir: Path, cfg: ChannelConfig, lines) -> list[dict]:
    from ..secretstore import RefError, parse

    out = []
    hub = hub_of(hub_dir)
    for path, value in _walk_refs(cfg.provider_config, "/provider_config"):
        if not isinstance(value, str):
            out.append(_err("invalid_config", path, "A reference is a string like "
                                                    f"secret:channel/{hub}/{cfg.id}/<name> or env:NAME.", lines))
            continue
        try:
            ref = parse(value)
        except RefError as exc:
            out.append(_err("invalid_config", path, str(exc), lines))
            continue
        if ref.provider == "secret" and ref.scope in ("hub", "channel") and ref.hub != hub:
            out.append(_err("invalid_config", path, f"A channel can use only this hub's secrets "
                                                    f"(secret:channel/{hub}/{cfg.id}/<name>).", lines))
        if ref.provider == "secret" and ref.scope == "channel" and ref.channel != cfg.id:
            out.append(_err("invalid_config", path, f"This channel's secrets are under "
                                                    f"secret:channel/{hub}/{cfg.id}/<name>.", lines))
    # a literal credential in a non-reference key is a mistake worth catching
    for key in ("token", "secret", "password", "private_key", "client_secret", "api_key"):
        if key in cfg.provider_config and cfg.provider_config[key]:
            out.append(_err("invalid_config", f"/provider_config/{key}",
                            f"Never put a credential in the file. Use {key}_ref with a secret: or env: "
                            "reference.", lines))
    return out


def _check_target(hub_dir: Path, cfg: ChannelConfig, lines) -> list[dict]:
    out = []
    if cfg.agent is not None and not agent_matches(hub_dir, cfg.agent):
        out.append(_err("unknown_agent", "/agent",
                        f"This hub answers as one agent. Use agent: main (or {_main_name(hub_dir)!r}).", lines))
    if cfg.workflow is not None:
        try:
            from ..workflows.runtime import definition

            wf = definition(hub_dir, cfg.workflow)
        except Exception:  # noqa: BLE001 - a broken workflow folder is reported, not raised
            wf = None
        if wf is None:
            out.append(_err("unknown_workflow", "/workflow",
                            f"There is no workflow {cfg.workflow!r} in this hub's workflows folder.", lines))
        elif not (wf.extra or {}).get("work_items"):
            out.append(_err("unknown_workflow", "/workflow",
                            f"Workflow {cfg.workflow!r} must run work items to take channel requests. Add "
                            "work_items=True (or routes) to its @workflow declaration.", lines))
    return out


def _main_name(hub_dir: Path) -> str:
    try:
        from ..loaders import agents as agents_loader

        return agents_loader.load_main(hub_dir).spec.name
    except Exception:  # noqa: BLE001
        return Path(hub_dir).name


def agent_matches(hub_dir: Path, name: str) -> bool:
    """A channel answers with the hub's main agent: `main`, the hub key or the
    main agent's name all name it."""
    want = name.strip().lower()
    candidates = {"main", hub_of(hub_dir), _main_name(hub_dir).strip().lower(),
                  re.sub(r"[^a-z0-9]+", "-", _main_name(hub_dir).lower()).strip("-")}
    return want in candidates


# -- loading files ------------------------------------------------------------------

def load_text(hub_dir, text: str, *, path: str | None = None, expected_id: str | None = None) -> Validation:
    try:
        loaded = yamlfile.loads(text, path=path)
    except YamlError as exc:
        return Validation(False, [{"code": "invalid_config", "field": exc.path or "/", "line": exc.line,
                                   "message": exc.message}])
    return validate(hub_dir, loaded.data, lines=loaded.lines, path=path, expected_id=expected_id)


def load_file(hub_dir, path: Path, *, expected_id: str | None = None) -> Validation:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return Validation(False, [{"code": "invalid_config", "field": "/", "line": None,
                                   "message": f"Cannot read {path}: {exc.strerror}."}])
    return load_text(hub_dir, text, path=str(path), expected_id=expected_id)


def render(data: dict) -> str:
    """The canonical YAML for a configuration, version first."""
    order = ["version", "id", "provider", "enabled", "agent", "workflow", "run_as", "allowed_groups",
             "queue_limit", "subscriptions", "progress", "provider_config"]
    ordered = {k: data[k] for k in order if k in data and data[k] is not None}
    ordered.update({k: v for k, v in data.items() if k not in ordered and v is not None})
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- the active connection ------------------------------------------------------------

@dataclass
class Connection:
    """An applied connection as the runtime uses it."""

    hub_dir: Path
    hub: str
    config: ChannelConfig
    settings: BaseModel
    revision: int
    status: str
    digest: str
    state: dict

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def provider(self) -> str:
        return self.config.provider

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def adapter(self):
        return registry.get(self.config.provider)

    def secret(self, field_name: str, *, required: bool = True) -> str | None:
        """The value of a `<field>_ref` setting, resolved on the host only."""
        from ..secretstore import resolve

        ref = getattr(self.settings, field_name, None)
        if not ref:
            if required:
                raise ChannelError("credential_missing", f"{self.id} has no {field_name} reference. Add it to "
                                   f"channels/{self.id}.yaml.", status=503, field=field_name)
            return None
        try:
            return resolve(ref, self.hub_dir)
        except Exception as exc:  # noqa: BLE001 - SecretError and friends
            if not required:
                return None
            code = getattr(exc, "code", "credential_missing")
            raise ChannelError("credential_missing" if code == "secret_unavailable" else code,
                               f"The secret {ref} for {self.id} is not set. Fill it with "
                               f"hubzoid secrets set --ref {ref} --stdin --apply.", status=503,
                               field=field_name) from None


def _connection(hub_dir: Path, row: dict) -> Connection:
    data = row["config_json"]
    cfg = ChannelConfig.model_validate(data)
    adapter = registry.get(cfg.provider)
    settings = adapter.validate(cfg.provider_config, hub_dir=hub_dir, connection_id=cfg.id)
    return Connection(hub_dir=Path(hub_dir), hub=row["hub"], config=cfg, settings=settings,
                      revision=row["revision"], status=row["status"], digest=row["digest"],
                      state=row.get("state_json") or {})


def active(hub_dir, cid: str) -> Connection:
    hub_dir = Path(hub_dir)
    row = store.get_connection(hub_dir, hub_of(hub_dir), cid)
    if row is None:
        raise not_found("channel")
    return _connection(hub_dir, row)


def active_all(hub_dir) -> list[Connection]:
    hub_dir = Path(hub_dir)
    out = []
    for row in store.list_connections(hub_dir, hub_of(hub_dir)):
        try:
            out.append(_connection(hub_dir, row))
        except Exception:  # noqa: BLE001 - one broken row never hides the others
            continue
    return out


def apply(hub_dir, data: dict, *, expected_revision, actor: str, text: str | None = None,
          lines: dict | None = None, expected_id: str | None = None) -> dict:
    """Validate, write `channels/<id>.yaml` and record the new active revision,
    as one step. `expected_revision` is an int or "absent". Returns
    `{id, revision, status, created, file}`."""
    hub_dir = Path(hub_dir)
    result = validate(hub_dir, data, lines=lines, expected_id=expected_id)
    if not result.valid:
        raise ChannelError("validation_failed", "The channel configuration is not valid. Fix the listed "
                           "fields and apply again.", status=422, fields=result.errors)
    cfg, settings = result.config, result.settings
    hub = hub_of(hub_dir)
    current = store.get_connection(hub_dir, hub, cfg.id)
    if current is not None and current["provider"] != cfg.provider:
        raise ChannelError("invalid_config", "A connection's provider never changes. Create a new connection "
                           "with another id.", status=422, fields=[_err("invalid_config", "/provider",
                                                                         "provider is fixed")])
    norm = normalized(cfg, settings)
    digest = canonical.digest(norm)
    path = file_for(hub_dir, cfg.id)
    body = text if text is not None else render(data)
    status = "enabled" if cfg.enabled else "disabled"
    with store.locked(hub_dir, f"channel:{hub}:{cfg.id}") as conn:
        out = store.put_connection(conn, hub=hub, cid=cfg.id, provider=cfg.provider, config=norm,
                                   digest=digest, expected_revision=expected_revision, status=status,
                                   file_path=str(path.relative_to(hub_dir)) if path.is_relative_to(hub_dir)
                                   else str(path), actor=actor)
        try:
            _write_atomic(path, body)
        except OSError as exc:
            raise ChannelError("hub_read_only", f"Cannot write {path}: {exc.strerror}. The hub folder must "
                               "be writable to apply channel changes.", status=503) from exc
    audit(hub_dir, actor, "channel_apply", f"{cfg.id}@{out['revision']}")
    return {"id": cfg.id, "revision": out["revision"], "status": status, "created": out["created"],
            "file": str(path), "digest": digest}


def export(hub_dir, cid: str) -> dict:
    """The active configuration with references, never values."""
    hub_dir = Path(hub_dir)
    row = store.get_connection(hub_dir, hub_of(hub_dir), cid)
    if row is None:
        raise not_found("channel")
    path = file_for(hub_dir, cid)
    on_disk = None
    if path.is_file():
        v = load_file(hub_dir, path, expected_id=cid)
        on_disk = canonical.digest(normalized(v.config, v.settings)) if v.valid else "invalid"
    return {"id": cid, "provider": row["provider"], "revision": row["revision"], "status": row["status"],
            "config": row["config_json"], "digest": row["digest"], "file": row["file_path"],
            "file_matches": on_disk == row["digest"] if on_disk else None,
            "applied_by": row["applied_by"], "updated": row["updated"]}


def set_enabled(hub_dir, cid: str, enabled: bool, *, expected_revision: int, actor: str) -> dict:
    """Pause or resume: rewrite `enabled` in the file and apply it. The one
    disable rule for every adapter."""
    current = export(hub_dir, cid)
    data = json.loads(json.dumps(current["config"]))
    data["enabled"] = bool(enabled)
    return apply(hub_dir, data, expected_revision=expected_revision, actor=actor, expected_id=cid)


def audit(hub_dir, actor: str, action: str, subject: str, *, request_id: str | None = None) -> None:
    """One access-audit row (conventions section 12). Best effort: the row it
    describes is the record."""
    try:
        from ..access import store_for

        store_for(Path(hub_dir)).audit_event(actor or "system", action, subject=subject,
                                             hub=hub_of(hub_dir), surface="channels", request_id=request_id)
    except Exception:  # noqa: BLE001 - the audit log never blocks a change
        pass
