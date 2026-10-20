"""`hubzoid upgrade`: write every setting that lives only in the database into
its one home, then bring the database to the current schema.

Run once, offline, after a backup, when moving a deployment to one run mode
(proposal 2026-10-19-one-run-mode, A9 and C2). It never overwrites a file that
exists and never prints a secret:

* ``hubzoid.yaml`` in the deployment folder, from a gateway's manifest (hub
  list, public URL, sign-in, owner, name) when there is none;
* the published brand (and its files under ``branding/``), chat defaults and
  diagnostics limits into its ``brand:``, ``chat:`` and ``diagnostics:``;
* connectors defined in Settings into ``connectors/<id>.yaml``, their secrets
  into the deployment's ``.env``;
* keys typed into Settings into the ``.env`` of their hub (or the deployment);
* a channel file for each 1.x channel a hub's ``.env`` turns on today;
* Workspace drafts and published versions, and channel settings kept only in
  the database, into ``<state>/exports/`` to review and commit.

Then it records that the export happened (``one_home_export`` in ``hz_meta``)
and runs the migrations, whose cleanup (``op_0080``) refuses to drop anything
before that.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

import yaml
from sqlalchemy import inspect, text

log = logging.getLogger("hubzoid.upgrade")
MARKER = "one_home_export"


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    return [dict(r._mapping) for r in conn.execute(text(sql), params or {})]


def _compact(value):
    """Leave out what says nothing (None, empty strings, lists and mappings),
    so the written section shows only what was set."""
    if isinstance(value, dict):
        out = {k: _compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _append_env(path: Path, values: dict[str, str]) -> list[str]:
    """Add NAME=value lines a file doesn't have yet. Returns the names added."""
    from dotenv import dotenv_values

    have = dotenv_values(path) if path.is_file() else {}
    new = {k: v for k, v in values.items() if v and not (have.get(k) or "").strip()}
    if not new:
        return []
    created = not path.exists()
    with open(path, "a", encoding="utf-8") as fh:
        if not created and path.read_text(encoding="utf-8")[-1:] not in ("", "\n"):
            fh.write("\n")
        fh.write("# moved here by `hubzoid upgrade` (one home for every setting)\n")
        for k, v in new.items():
            quoted = v if all(c.isalnum() or c in "-_./:+=@" for c in v) else json.dumps(v)
            fh.write(f"{k}={quoted}\n")
    if created:
        os.chmod(path, 0o600)
    return sorted(new)


class Upgrade:
    def __init__(self, root: Path, state: Path, hub_dirs: list[Path], engine, *, dry_run: bool = False,
                 say: Callable[[str], None] = print):
        self.root, self.state, self.hubs = Path(root), Path(state), [Path(h) for h in hub_dirs]
        self.engine, self.dry_run, self.say = engine, dry_run, say
        self.done: list[str] = []
        self.doc: dict = {}
        self.doc_changed = False
        self.from_manifest: set[str] = set()   # sections only the manifest filled

    def note(self, text_: str) -> None:
        self.done.append(text_)
        self.say(("would: " if self.dry_run else "") + text_)

    # -- the deployment file ---------------------------------------------------------
    def _deployment_file(self) -> None:
        path = self.root / "hubzoid.yaml"
        if path.is_file():
            self.doc = yaml.safe_load(path.read_text()) or {}
            return
        manifest = {}
        try:
            manifest = json.loads((self.state / "deployment.json").read_text())
        except (OSError, ValueError):
            pass
        hubs = [os.path.relpath(h.resolve(), self.root.resolve()) for h in self.hubs]
        self.doc = {"version": 1}
        if manifest.get("public_url"):
            self.doc["public_url"] = manifest["public_url"]
        if manifest.get("auth") is not None:
            self.doc["sign_in"] = bool(manifest["auth"])
        if manifest.get("owner"):
            self.doc["owner"] = manifest["owner"]
        self.doc["hubs"] = hubs
        if manifest.get("name") and manifest["name"] != "Hubzoid":
            self.doc["brand"] = {"identity": {"name": manifest["name"]}}
            self.from_manifest.add("brand")
        self.doc_changed = True
        self.note(f"write {path} (hubs: {', '.join(hubs)})")

    # -- brand, chat defaults, diagnostics -------------------------------------------------
    def _section(self, conn, tables: set, key: str, legacy: str, sql: str, column: str, fix=None) -> None:
        if (key in self.doc and key not in self.from_manifest) or (self.root / legacy).is_file():
            return
        if not tables.issuperset(sql.split("FROM ")[1].split()[0:1]):
            return
        row = next(iter(_rows(conn, sql)), None)
        if row is None:
            return
        data = json.loads(row[column])
        data.pop("version", None)
        if fix:
            data = fix(data, row)
        data = _compact(data)
        if key == "brand" and isinstance(self.doc.get("brand"), dict):
            data = {**data, "identity": {**self.doc["brand"].get("identity", {}), **data.get("identity", {})}}
        self.doc[key] = data
        self.doc_changed = True
        self.note(f"move the {key} kept in the database into hubzoid.yaml ({key}:)")

    def _brand_files(self, conn, tables: set) -> None:
        if "hz_branding_active" not in tables:
            return
        row = next(iter(_rows(conn, "SELECT r.theme_json, r.assets_json FROM hz_branding_active a "
                                    "JOIN hz_branding_revisions r ON r.id = a.revision_id")), None)
        if row is None:
            return
        theme = json.loads(row["theme_json"] or "null")
        theme_path = self.root / "branding" / "theme.json"
        if theme and not theme_path.exists():
            self.note(f"write {theme_path}")
            if not self.dry_run:
                theme_path.parent.mkdir(parents=True, exist_ok=True)
                theme_path.write_text(json.dumps(theme, indent=2) + "\n")
        stores = [self.state / ".hubzoid" / "branding" / "assets",
                  *[h / ".hubzoid" / ".hubzoid" / "branding" / "assets" for h in self.hubs],
                  *[h / ".hubzoid" / "branding" / "assets" for h in self.hubs]]
        for a in json.loads(row["assets_json"] or "[]"):
            if a.get("generated"):
                continue
            target = self.root / a["path"]
            if target.exists():
                continue
            hexd = str(a["digest"]).split(":", 1)[-1]
            source = next((d / hexd for d in stores if (d / hexd).is_file()), None)
            if source is None:
                self.note(f"brand file {a['path']} is missing from the asset store; add it by hand")
                continue
            self.note(f"write {target}")
            if not self.dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())

    # -- connectors and keys ---------------------------------------------------------
    def _connectors(self) -> None:
        from .connectors import registry

        try:
            stored = registry.stored_definitions(self.hubs[0], eng=self.engine)
        except Exception:  # noqa: BLE001 - no registry table yet
            return
        env_values: dict[str, str] = {}
        for c in stored:
            path = self.root / "connectors" / f"{c['id']}.yaml"
            if path.exists():
                continue
            doc = {"version": 1, "name": c["name"], "url": c["url"], "sign_in": c["auth_type"] or "oauth",
                   "hubs": c["hubs"]}
            if c.get("client_id"):
                doc["client_id"] = c["client_id"]
            if c.get("scopes"):
                doc["scopes"] = c["scopes"]
            if c.get("tool_allowlist"):
                try:
                    doc["tools"] = json.loads(c["tool_allowlist"])
                except ValueError:
                    pass
            if not c.get("enabled", 1):
                doc["enabled"] = False
            if c.get("shared_header"):
                doc["shared_header"] = c["shared_header"]
            for field, out in (("client_secret", "client_secret"), ("shared_secret", "shared_key")):
                if c.get(field):
                    name = f"HZ_CONNECTOR_{c['id'].upper()}_{out.upper()}"
                    env_values[name] = c[field]
                    doc[out] = f"env:{name}"
            self.note(f"write {path}")
            if not self.dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(yaml.safe_dump(doc, sort_keys=False))
        if env_values:
            self.note(f"add {len(env_values)} connector secret(s) to {self.root / '.env'} (names only shown): "
                      + ", ".join(sorted(env_values)))
            if not self.dry_run:
                _append_env(self.root / ".env", env_values)

    def _typed_keys(self) -> None:
        from .assertions import hub_key
        from .secretstore import store

        targets = [("deployment", "", self.root / ".env")] + [("hub", hub_key(h), h / ".env") for h in self.hubs]
        for scope, target, env_path in targets:
            try:
                items = store.values(self.hubs[0], scope=scope, target=target, eng=self.engine)
            except Exception:  # noqa: BLE001 - no store table
                continue
            values = {i.meta.name: i.value for i in items if i.value}
            if values:
                self.note(f"move {len(values)} typed key(s) to {env_path}: {', '.join(sorted(values))}")
                if not self.dry_run:
                    _append_env(env_path, values)

    # -- channels ----------------------------------------------------------------------
    def _channel_files(self) -> None:
        from . import gateway as gateway_lib
        from .channels import legacy

        for hub in self.hubs:
            plan = legacy.plan(hub, gateway_lib._own_env(hub))
            for note in plan.notes:
                provider = note.split("the ", 1)[1].split(" settings", 1)[0]
                path = hub / "channels" / f"{legacy.SUGGESTED[provider]}.yaml"
                if path.exists():
                    continue
                self.note(f"write {path}")
                if not self.dry_run:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(legacy.file_text(provider))

    def _exports(self, conn, tables: set) -> None:
        out = self.state / "exports"
        if "hz_authoring_drafts" in tables:
            for r in _rows(conn, "SELECT id, hub_id, path, content, state FROM hz_authoring_drafts "
                                 "WHERE state NOT IN ('published', 'discarded')"):
                self._export_file(out / "workspace-drafts" / r["hub_id"] / f"{r['id']}__{r['path'].replace('/', '__')}",
                                  r["content"])
        if "hz_authoring_versions" in tables:
            for r in _rows(conn, "SELECT id, hub_id, path, content, published_at FROM hz_authoring_versions"):
                stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(r["published_at"] or 0))
                self._export_file(out / "workspace-versions" / r["hub_id"] /
                                  f"{stamp}-{r['id'][:8]}__{r['path'].replace('/', '__')}", r["content"])
        if "hz_channel_connections" in tables:
            for r in _rows(conn, "SELECT hub, id, config_json, file_path FROM hz_channel_connections"):
                hub = next((h for h in self.hubs if h.name.lower() == r["hub"]), None)
                if hub is not None and (hub / (r["file_path"] or f"channels/{r['id']}.yaml")).exists():
                    continue
                self._export_file(out / "channels" / r["hub"] / f"{r['id']}.yaml",
                                  yaml.safe_dump(json.loads(r["config_json"]), sort_keys=False))

    def _export_file(self, path: Path, content: str) -> None:
        self.note(f"export {path}")
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    # -- the run ---------------------------------------------------------------------
    def run(self) -> list[str]:
        from . import migrations

        tables = set(inspect(self.engine).get_table_names())
        self._deployment_file()
        with self.engine.connect() as conn:
            self._section(conn, tables, "brand", "branding.yaml",
                          "SELECT r.configuration_json FROM hz_branding_active a JOIN hz_branding_revisions r "
                          "ON r.id = a.revision_id", "configuration_json")
            self._section(conn, tables, "chat", "chat/config.yaml",
                          "SELECT r.document_json FROM hz_chat_config_heads h JOIN hz_chat_config_revisions r "
                          "ON r.id = h.revision_id", "document_json")
            self._section(conn, tables, "diagnostics", "admin.yaml",
                          "SELECT r.document_json FROM hz_admin_config_heads h JOIN hz_admin_config_revisions r "
                          "ON r.id = h.revision_id", "document_json",
                          fix=lambda data, row: data.get("diagnostics") or {})
            self._brand_files(conn, tables)
            self._exports(conn, tables)
        self._connectors()
        self._typed_keys()
        self._channel_files()
        if self.doc_changed:
            path = self.root / "hubzoid.yaml"
            if not self.dry_run:
                path.write_text(yaml.safe_dump(self.doc, sort_keys=False))
        if self.dry_run:
            return self.done
        with self.engine.begin() as conn:
            if "hz_meta" in tables or "hz_meta" in set(inspect(self.engine).get_table_names()):
                conn.execute(text("DELETE FROM hz_meta WHERE k = :k"), {"k": MARKER})
                conn.execute(text("INSERT INTO hz_meta (k, v) VALUES (:k, :v)"),
                             {"k": MARKER, "v": json.dumps({"at": time.time(), "items": len(self.done)})})
        migrations.upgrade(self.engine, "operational")
        self.note("database at the current schema")
        return self.done
