"""The deployment's brand, from its one home: the folder.

The brand is the ``brand:`` section of ``hubzoid.yaml`` (``branding.yaml`` in
the deployment folder for this release, when there is none), with its images
and ``theme.json`` under ``branding/`` next to it. Every bridge reads them again
when they change (``hubzoid.control.filesettings``). History is the folder's
Git history; there are no drafts, reviews or activations, and Settings shows
the brand read-only with the file to edit.

Referenced images are checked (``branding_assets``: SVG that only draws,
re-encoded rasters, sizes) and kept by content digest under the deployment's
state folder, so they are served at ``/branding/assets/<deployment>/<digest>/<name>``
with immutable caching. Missing installation icons are generated from a raster
favicon or logo.

What stays in the database is each person's: their acceptance of the current
terms (``hz_branding_legal``) and the announcements they dismissed
(``hz_branding_dismissals``).

Without a brand file the deployment shows the ``branding/`` folder's name, logo
and favicon (never its ``custom.css``).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ..chat import theme as theme_mod
from ..control import canonical, deploy, yamlfile
from . import branding_assets as assets_mod
from . import branding_css
from . import branding_models as models
from .branding_models import BrandError

log = logging.getLogger("hubzoid.ui.branding")

metadata = sa.MetaData()

legal = sa.Table(
    "hz_branding_legal", metadata,
    sa.Column("deployment_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("policy_hash", sa.String(80), primary_key=True),
    sa.Column("policy_version", sa.String(80), nullable=False),
    sa.Column("accepted_at", sa.Float, nullable=False),
)

dismissals = sa.Table(
    "hz_branding_dismissals", metadata,
    sa.Column("deployment_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("announcement_id", sa.String(80), primary_key=True),
    sa.Column("dismissed_at", sa.Float, nullable=False),
)


def _hexdigest(digest: str) -> str:
    return digest.split(":", 1)[-1]


class BrandingService:
    def __init__(self, hub_dir: Path, engine=None):
        from .. import db, migrations
        from ..control.filesettings import FileSection, settings_root

        self.hub_dir = Path(hub_dir)
        self.root = settings_root(self.hub_dir)
        self.deployment_id = deploy.deployment_id(self.hub_dir)
        if engine is None:
            engine = db.operational_engine(self.hub_dir)
            migrations.upgrade(engine, "operational")
        self.engine = engine
        self.asset_dir = deploy.state_root(self.hub_dir) / ".hubzoid" / "branding" / "assets"
        self._file = FileSection(self.hub_dir, "brand", models.FILE, parse=self._parse,
                                 default=lambda: {}, error=lambda m: BrandError(503, "brand_invalid", m))
        self._lock = threading.Lock()
        self._cache: tuple | None = None   # (stamp, configuration, theme, assets)

    @staticmethod
    def _parse(data, loaded) -> dict:
        if loaded is None:  # the `brand:` section of hubzoid.yaml
            data = {"version": 1, **(data or {})} if isinstance(data, dict) or data is None else data
        return models.validate(data)

    # -- assets -------------------------------------------------------------------
    def _store_asset(self, checked: assets_mod.Checked) -> dict:
        dig = canonical.digest_bytes(checked.data)
        key = _hexdigest(dig)
        target = self.asset_dir / key
        if not target.exists():
            self.asset_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(checked.data)
            tmp.replace(target)
            (self.asset_dir / f"{key}.type").write_text(checked.media_type)
        return {"digest": dig, "media_type": checked.media_type, "size": len(checked.data),
                "width": checked.width, "height": checked.height, "extension": checked.extension}

    def asset_bytes(self, digest: str) -> tuple[bytes, str] | None:
        key = _hexdigest(digest)
        try:
            return (self.asset_dir / key).read_bytes(), (self.asset_dir / f"{key}.type").read_text().strip()
        except OSError:
            return None

    def _ingest(self, configuration: dict, files: dict[str, bytes]) -> tuple[list[dict], list[str], str | None]:
        """Check the referenced files that were supplied. Returns the asset
        list, the referenced paths still missing, and the decorative CSS."""
        refs = models.referenced_paths(configuration)
        listed: list[dict] = []
        missing: list[str] = []
        css_text = None
        total = 0
        if len(refs) > assets_mod.MAX_FILES:
            raise BrandError(422, "validation_failed", "A brand can use at most 64 files.")
        for key, path in sorted(refs.items()):
            data = files.get(path)
            if data is None:
                missing.append(path)
                continue
            total += len(data)
            if total > assets_mod.MAX_TOTAL_BYTES:
                raise BrandError(413, "asset_too_large", "Brand files add up to more than 20 MiB.")
            if key == "decorative_css":
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    raise BrandError(422, "unsafe_style", f"{path} is not UTF-8 text.", pointer=path)
                rules = branding_css.parse(text, path)
                css_text = branding_css.render(rules)
                checked = assets_mod.Checked(css_text.encode("utf-8"), "text/css", None, None, "css")
            else:
                checked = assets_mod.check(key, data, path)
            listed.append({"key": key, "path": path, **self._store_asset(checked)})
        # Installation icons the brand didn't supply, from a raster favicon or logo.
        have = {a["key"] for a in listed}
        master = next((a for a in listed if a["key"] in ("favicon", "logo_light")
                       and a["media_type"] != "image/svg+xml"), None)
        if master is not None:
            master_bytes = (self.asset_dir / _hexdigest(master["digest"])).read_bytes()
            for key, size in assets_mod.ICON_SIZES.items():
                if key in have or key in refs:
                    continue
                png = assets_mod.generate_icon(master_bytes, size)
                checked = assets_mod.Checked(png, "image/png", size, size, "png")
                listed.append({"key": key, "path": f"generated/{key}.png", "generated": True,
                               **self._store_asset(checked)})
        listed.sort(key=lambda a: a["key"])
        return listed, missing, css_text

    # -- the folder -----------------------------------------------------------------
    def _folder(self):
        """(configuration, theme, assets, source) of the brand file, or None
        when the deployment has none. Raises BrandError when it is broken and
        nothing good was read before."""
        cur = self._file.current()
        if cur.path is None:
            return None
        configuration = cur.document
        theme_path = self.root / models.THEME_FILE
        refs = models.referenced_paths(configuration)
        stamp_parts = [cur.digest]
        for rel in [models.THEME_FILE, *refs.values()]:
            try:
                st = (self.root / rel).stat()
                stamp_parts.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                stamp_parts.append(f"{rel}:-")
        stamp = "|".join(stamp_parts)
        with self._lock:
            if self._cache is not None and self._cache[0] == stamp:
                return self._cache[1:]
            theme = None
            if theme_path.is_file():
                try:
                    theme = theme_mod.validate_manifest(json.loads(theme_path.read_text(encoding="utf-8")))
                except (ValueError, theme_mod.ThemeError) as exc:
                    log.warning("branding: %s is not usable (%s); using the default theme", models.THEME_FILE, exc)
            files: dict[str, bytes] = {}
            for rel in refs.values():
                try:
                    target = deploy.resolve_inside(self.root, rel)
                except ValueError:
                    continue
                if target.is_file():
                    files[rel] = target.read_bytes()
            try:
                assets, missing, _css = self._ingest(configuration, files)
            except BrandError as exc:
                log.warning("branding: %s", exc.message)
                assets, missing = [], []
            if missing:
                log.warning("branding: %s names files that aren't there: %s", cur.source, ", ".join(missing))
            self._cache = (stamp, configuration, theme, assets, cur)
            return self._cache[1:]

    def active_state(self) -> dict:
        got = self._folder()
        if got is None:
            return {"revision": None, "generation": 0, "read_only": True}
        _c, _t, _a, cur = got
        return {"revision": cur.digest, "generation": 1, "digest": cur.digest, "created_at": cur.loaded_at,
                "source": cur.source, "valid": cur.error is None, "error": cur.error, "read_only": True}

    def active_source(self) -> dict | None:
        got = self._folder()
        if got is None:
            return None
        configuration, theme, assets, cur = got
        return {"revision": cur.digest, "generation": 1, "digest": cur.digest, "configuration": configuration,
                "theme": theme, "assets": assets, "source": cur.source, "error": cur.error, "read_only": True}

    def active_files(self) -> dict[str, bytes]:
        src = self.active_source()
        out: dict[str, bytes] = {}
        for a in (src or {}).get("assets", []):
            if a.get("generated"):
                continue
            got = self.asset_bytes(a["digest"])
            if got is not None:
                out[a["path"]] = got[0]
        return out

    def _css_contrast(self, path: str, css_text: str, resolved: dict) -> list[dict]:
        """A decorative background must keep the slot's text readable in every
        mode (one fixed colour behind light and dark text usually can't)."""
        rules = branding_css.parse(css_text, path)
        css_asset = {"path": path}
        out = []
        for slot, decls in rules.items():
            bg = decls.get("background-color")
            if not bg:
                continue
            for mode in theme_mod.MODES:
                pal = resolved["palettes"][mode]
                for token in ("text", "mutedText"):
                    ratio = theme_mod.contrast(pal[token], bg.upper())
                    if ratio < 4.5:
                        out.append({"code": "contrast_failure", "pointer": css_asset["path"],
                                    "message": (f"{css_asset['path']}: {slot} background {bg} makes {token} "
                                                f"unreadable in {mode} mode ({ratio:.2f}:1, needs 4.5:1). Remove "
                                                "background-color or choose a colour that works in every mode."),
                                    "failures": [{"mode": mode, "foreground": token, "background": slot,
                                                  "ratio": round(ratio, 2), "minimum": 4.5}]})
                        break
        return out

    def _resolved(self, configuration: dict, theme: dict | None, assets: list[dict], url_for) -> dict:
        resolved_theme = theme_mod.resolve(theme, check=False) if theme else theme_mod.resolve()
        urls = {a["key"]: url_for(a) for a in assets}
        legal_cfg = configuration["legal"]
        links = configuration["links"]
        policy_hash = (canonical.digest({"version": legal_cfg["version"], "terms": links.get("terms")})
                       if legal_cfg.get("require_acceptance") else None)
        return {
            "identity": configuration["identity"],
            "assets": urls,
            "copy": configuration.get("copy", {}).get("en") or {},
            "links": links,
            "legal": {**legal_cfg, "policy_hash": policy_hash},
            "announcement": configuration.get("announcement"),
            "presentation": {k: v for k, v in configuration["presentation"].items() if k != "decorative_css"},
            "decorative_css_url": urls.get("decorative_css"),
            "theme": theme_mod.public(resolved_theme),
        }

    # -- public view --------------------------------------------------------------
    def public_id(self) -> str:
        return self.deployment_id

    def active_manifest(self) -> dict | None:
        got = self._folder()
        if got is None:
            return None
        configuration, theme, assets, cur = got
        base = f"/branding/assets/{self.public_id()}"
        out = self._resolved(configuration, theme, assets,
                             lambda a: f"{base}/{_hexdigest(a['digest'])}/{a['key']}.{a.get('extension', 'bin')}")
        return {**out, "revision": cur.digest, "generation": 1}

    def public_asset(self, hexdigest: str) -> tuple[bytes, str] | None:
        """A file of the current brand."""
        got = self._folder()
        if got is None or not any(_hexdigest(a["digest"]) == hexdigest for a in got[2]):
            return None
        return self.asset_bytes(hexdigest)

    def active_theme(self) -> tuple[str, dict] | None:
        got = self._folder()
        if got is None:
            return None
        _c, theme, _a, cur = got
        return cur.digest, (theme_mod.resolve(theme, check=False) if theme else theme_mod.resolve())

    # -- per person ------------------------------------------------------------------
    def me(self, user_id: str) -> dict:
        manifest = self.active_manifest()
        with self.engine.connect() as conn:
            accepted = {r.policy_hash for r in conn.execute(sa.select(legal.c.policy_hash).where(
                legal.c.deployment_id == self.deployment_id, legal.c.user_id == user_id))}
            dismissed = {r.announcement_id for r in conn.execute(sa.select(dismissals.c.announcement_id).where(
                dismissals.c.deployment_id == self.deployment_id, dismissals.c.user_id == user_id))}
        policy = (manifest or {}).get("legal", {}).get("policy_hash")
        return {"terms_required": bool(policy) and policy not in accepted, "policy_hash": policy,
                "dismissed_announcements": sorted(dismissed)}

    def accept_terms(self, user_id: str, *, version: str, policy_hash: str) -> dict:
        manifest = self.active_manifest() or {}
        current = manifest.get("legal", {})
        if not current.get("policy_hash") or current["policy_hash"] != policy_hash or current.get("version") != version:
            raise BrandError(409, "revision_conflict", "The terms changed. Read the current version first.")
        with self.engine.begin() as conn:
            exists = conn.execute(sa.select(legal.c.user_id).where(
                legal.c.deployment_id == self.deployment_id, legal.c.user_id == user_id,
                legal.c.policy_hash == policy_hash)).first()
            if exists is None:
                conn.execute(legal.insert().values(deployment_id=self.deployment_id, user_id=user_id,
                                                   policy_hash=policy_hash, policy_version=version,
                                                   accepted_at=time.time()))
        return {"accepted": True, "policy_hash": policy_hash, "version": version}

    def dismiss(self, user_id: str, announcement_id: str) -> dict:
        with self.engine.begin() as conn:
            exists = conn.execute(sa.select(dismissals.c.user_id).where(
                dismissals.c.deployment_id == self.deployment_id, dismissals.c.user_id == user_id,
                dismissals.c.announcement_id == announcement_id)).first()
            if exists is None:
                conn.execute(dismissals.insert().values(deployment_id=self.deployment_id, user_id=user_id,
                                                        announcement_id=announcement_id, dismissed_at=time.time()))
        return {"dismissed": announcement_id}

    # -- files -----------------------------------------------------------------------
    def read_source(self) -> tuple[dict, dict | None, dict[str, bytes]]:
        """branding.yaml, branding/theme.json and the files it references, from
        the deployment folder. Raises BrandError."""
        path = self.root / models.FILE
        try:
            loaded = yamlfile.load(path)
        except yamlfile.YamlError as exc:
            raise BrandError(422, "invalid_yaml", exc.message, file=exc.path, line=exc.line)
        configuration = models.validate(loaded.data)
        theme = None
        theme_path = self.root / models.THEME_FILE
        if theme_path.is_file():
            try:
                theme = json.loads(theme_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise BrandError(422, "validation_failed", f"{models.THEME_FILE} is not valid JSON: {exc}.")
        files: dict[str, bytes] = {}
        for _key, rel in models.referenced_paths(configuration).items():
            try:
                target = deploy.resolve_inside(self.root, rel)
            except ValueError as exc:
                raise BrandError(422, "validation_failed", f"{rel}: {exc}", pointer=rel)
            if target.is_file():
                files[rel] = target.read_bytes()
        return configuration, theme, files

    def legacy_import(self) -> tuple[dict, dict[str, bytes], list[dict]]:
        """A branding.yaml built from a pre-2.0 ``branding/`` folder, and the
        disposition of every file found there."""
        from ..webapp_gateway import branding_scope, _find, _LOGO_CANDIDATES, _FAVICON_CANDIDATES

        scope = branding_scope(self.hub_dir, None)
        folder = scope.folder
        report: list[dict] = []
        configuration: dict = {"version": 1, "identity": {"name": scope.name[:80] or "Hubzoid"}, "assets": {}}
        files: dict[str, bytes] = {}
        if folder is None or not folder.is_dir():
            return configuration, files, report
        logo = _find(folder, _LOGO_CANDIDATES, custom=True)
        favicon = _find(folder, _FAVICON_CANDIDATES)
        mapped: dict[str, list[str]] = {}
        for name, slot in ((logo, "logo_light"), (favicon, "favicon")):
            if name:
                rel = f"branding/{name}"
                configuration["assets"][slot] = rel
                files[rel] = (folder / name).read_bytes()
                mapped.setdefault(name, []).append(f"assets.{slot}")
        for name, slots in mapped.items():
            report.append({"file": name, "disposition": "mapped", "to": " and ".join(slots)})
        # Files the current branding.yaml already uses, and the theme, are not legacy.
        in_use = {"theme.json"}
        try:
            current, _theme, _files = self.read_source()
            in_use |= {Path(p).name for p in models.referenced_paths(current).values()}
        except BrandError:
            pass
        for p in sorted(folder.iterdir()):
            if not p.is_file() or p.name in mapped or p.name in in_use:
                continue
            if p.name.lower() == "custom.css":
                report.append({"file": p.name, "disposition": "dropped",
                               "reason": "Unrestricted CSS is no longer served. Move decoration to "
                                         "presentation.decorative_css (brand slots only)."})
            else:
                report.append({"file": p.name, "disposition": "not_used",
                               "reason": "Not a 2.0 brand slot. Reference it in branding.yaml to use it."})
        return configuration, files, report


_services: dict[str, BrandingService] = {}
_lock = threading.Lock()


def service_for(hub_dir: Path) -> BrandingService:
    key = str(Path(hub_dir).resolve())
    with _lock:
        svc = _services.get(key)
        if svc is None:
            svc = BrandingService(Path(hub_dir))
            _services[key] = svc
        return svc


def active_theme(hub_dir: Path) -> tuple[str, dict] | None:
    return service_for(hub_dir).active_theme()
