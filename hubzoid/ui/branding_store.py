"""Branding publication: draft, validate, preview, review, activate, roll back.

Nothing a builder saves reaches teammates until an exact candidate is reviewed
and activated:

1. A **draft** holds a complete candidate: the ``branding.yaml`` manifest, the
   ``theme.json`` tokens and every referenced asset (checked and stored by
   content digest). Its ``digest`` covers all of that plus the base revision
   and the validator version. Any edit bumps the draft's ``revision`` and
   invalidates earlier reviews.
2. A **review** records who approved (or rejected) which digest of which draft
   revision. A personal owner may approve their own draft. A review is bound
   to its digest: changed bytes, a changed base or a new validator need a
   fresh review.
3. **Activation** stores the immutable revision, then moves the deployment's
   active pointer by compare-and-set on its ``generation`` and records the
   activation and its audit row in the same transaction. Retrying with the
   same ``Idempotency-Key`` returns the same activation.
4. **Rollback** creates a new draft from a retained revision. It goes through
   review and activation like any other change, so history is never erased.

Activated assets are public (the sign-in page needs them) at
``/branding/assets/<deployment>/<digest>/<name>`` with immutable caching. Draft
assets are private. Without any activation the deployment shows the legacy
``branding/`` folder's name, logo and favicon (never its ``custom.css``).
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from ..chat import theme as theme_mod
from ..control import canonical, deploy, idempotency, yamlfile
from . import branding_assets as assets_mod
from . import branding_css
from . import branding_models as models
from .branding_models import BrandError

log = logging.getLogger("hubzoid.ui.branding")

metadata = sa.MetaData()

drafts = sa.Table(
    "hz_branding_drafts", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("base_revision", sa.String(64), nullable=False),
    sa.Column("configuration_json", sa.Text, nullable=False),
    sa.Column("theme_json", sa.Text, nullable=False),
    sa.Column("assets_json", sa.Text, nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("validation_json", sa.Text, nullable=False),
    sa.Column("creator", sa.String(320), nullable=False),
    sa.Column("validator_version", sa.String(32), nullable=False),
    sa.Column("source", sa.String(32), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

asset_rows = sa.Table(
    "hz_branding_assets", metadata,
    sa.Column("deployment_id", sa.String(64), primary_key=True),
    sa.Column("digest", sa.String(80), primary_key=True),
    sa.Column("media_type", sa.String(64), nullable=False),
    sa.Column("byte_size", sa.Integer, nullable=False),
    sa.Column("width", sa.Integer),
    sa.Column("height", sa.Integer),
    sa.Column("storage_key", sa.String(255), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

reviews = sa.Table(
    "hz_branding_reviews", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("draft_id", sa.String(64), nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("draft_revision", sa.Integer, nullable=False),
    sa.Column("base_revision", sa.String(64), nullable=False),
    sa.Column("validator_version", sa.String(32), nullable=False),
    sa.Column("actor", sa.String(320), nullable=False),
    sa.Column("decision", sa.String(16), nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("created_at", sa.Float, nullable=False),
)

revisions = sa.Table(
    "hz_branding_revisions", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("configuration_json", sa.Text, nullable=False),
    sa.Column("theme_json", sa.Text, nullable=False),
    sa.Column("assets_json", sa.Text, nullable=False),
    sa.Column("css_text", sa.Text),
    sa.Column("validator_version", sa.String(32), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("deployment_id", "digest", name="hz_branding_revisions_digest"),
)

active = sa.Table(
    "hz_branding_active", metadata,
    sa.Column("deployment_id", sa.String(64), primary_key=True),
    sa.Column("revision_id", sa.String(64), nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
)

activations = sa.Table(
    "hz_branding_activations", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("deployment_id", sa.String(64), nullable=False),
    sa.Column("from_revision", sa.String(64)),
    sa.Column("to_revision", sa.String(64), nullable=False),
    sa.Column("review_id", sa.String(64), nullable=False),
    sa.Column("actor", sa.String(320), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

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

NO_BASE = "none"
DRAFT_TTL = 30 * 24 * 3600


@dataclass
class Draft:
    id: str
    base_revision: str
    configuration: dict
    theme: dict | None
    assets: list[dict]
    digest: str
    revision: int
    state: str
    validation: dict
    creator: str
    source: str
    created_at: float
    updated_at: float

    def public(self, preview_base: str = "") -> dict:
        return {"id": self.id, "base_revision": self.base_revision, "revision": self.revision,
                "state": self.state, "digest": self.digest, "validation": self.validation,
                "creator": self.creator, "source": self.source, "created_at": self.created_at,
                "updated_at": self.updated_at, "configuration": self.configuration,
                "theme": self.theme, "assets": self.assets,
                "preview_url": f"/branding-preview/{self.id}"}


def _draft(row) -> Draft:
    return Draft(row.id, row.base_revision, json.loads(row.configuration_json),
                 json.loads(row.theme_json), json.loads(row.assets_json), row.digest, row.revision,
                 row.state, json.loads(row.validation_json), row.creator, row.source,
                 row.created_at, row.updated_at)


def _hexdigest(digest: str) -> str:
    return digest.split(":", 1)[1]


class BrandingService:
    def __init__(self, hub_dir: Path, engine=None):
        from .. import db, migrations

        self.hub_dir = Path(hub_dir)
        self.root = deploy.root(self.hub_dir)
        self.deployment_id = deploy.deployment_id(self.hub_dir)
        if engine is None:
            engine = db.operational_engine(self.hub_dir)
            migrations.upgrade(engine, "operational")
        self.engine = engine
        self.asset_dir = self.root / ".hubzoid" / "branding" / "assets"

    # -- assets -------------------------------------------------------------------
    def _store_asset(self, conn, checked: assets_mod.Checked) -> dict:
        dig = canonical.digest_bytes(checked.data)
        key = _hexdigest(dig)
        target = self.asset_dir / key
        if not target.exists():
            self.asset_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(checked.data)
            tmp.replace(target)
        exists = conn.execute(sa.select(asset_rows.c.digest).where(
            asset_rows.c.deployment_id == self.deployment_id, asset_rows.c.digest == dig)).first()
        if exists is None:
            conn.execute(asset_rows.insert().values(
                deployment_id=self.deployment_id, digest=dig, media_type=checked.media_type,
                byte_size=len(checked.data), width=checked.width, height=checked.height,
                storage_key=key, created_at=time.time()))
        return {"digest": dig, "media_type": checked.media_type, "size": len(checked.data),
                "width": checked.width, "height": checked.height, "extension": checked.extension}

    def asset_bytes(self, digest: str) -> tuple[bytes, str] | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(asset_rows).where(
                asset_rows.c.deployment_id == self.deployment_id, asset_rows.c.digest == digest)).first()
        if row is None:
            return None
        path = self.asset_dir / row.storage_key
        try:
            return path.read_bytes(), row.media_type
        except OSError:
            return None

    def _ingest(self, conn, configuration: dict, files: dict[str, bytes]) -> tuple[list[dict], list[str], str | None]:
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
            listed.append({"key": key, "path": path, **self._store_asset(conn, checked)})
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
                               **self._store_asset(conn, checked)})
        listed.sort(key=lambda a: a["key"])
        return listed, missing, css_text

    # -- read ---------------------------------------------------------------------
    def _active_row(self, conn):
        head = conn.execute(sa.select(active).where(active.c.deployment_id == self.deployment_id)).first()
        if head is None:
            return None, None
        rev = conn.execute(sa.select(revisions).where(revisions.c.id == head.revision_id)).first()
        return head, rev

    def active_state(self) -> dict:
        with self.engine.connect() as conn:
            head, rev = self._active_row(conn)
        if head is None:
            return {"revision": None, "generation": 0}
        return {"revision": rev.id, "generation": head.generation, "digest": rev.digest,
                "created_at": rev.created_at}

    def get_draft(self, draft_id: str) -> Draft:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(drafts).where(drafts.c.id == draft_id,
                                                       drafts.c.deployment_id == self.deployment_id)).first()
        if row is None:
            raise BrandError(404, "not_found", "That branding draft was not found.")
        return _draft(row)

    def list_drafts(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(drafts).where(drafts.c.deployment_id == self.deployment_id)
                                .order_by(drafts.c.updated_at.desc()).limit(50)).all()
        return [_draft(r).public() for r in rows]

    # -- drafts -------------------------------------------------------------------
    def _evaluate(self, configuration: dict, theme: dict | None, assets: list[dict],
                  missing: list[str], base: str) -> tuple[str, dict, str]:
        """(state, validation, digest) for a candidate."""
        problems: list[dict] = []
        if missing:
            problems.append({"code": "missing_asset", "message": "Upload these files: " + ", ".join(missing),
                             "paths": missing})
        try:
            theme_mod.resolve(theme) if theme else theme_mod.resolve()
        except theme_mod.ThemeError as exc:
            problems.append({"code": exc.code, "message": exc.message, "pointer": exc.pointer,
                             **({"failures": exc.extra["failures"]} if "failures" in exc.extra else {})})
        state = "valid" if not problems else "draft"
        dig = canonical.digest({
            "configuration": configuration, "theme": theme,
            "assets": sorted(({"key": a["key"], "path": a["path"], "digest": a["digest"]} for a in assets),
                             key=lambda a: a["key"]),
            "base_revision": base, "validator_version": models.VALIDATOR_VERSION})
        return state, {"valid": not problems, "problems": problems,
                       "validator_version": models.VALIDATOR_VERSION}, dig

    def create_draft(self, configuration: Any, theme: Any, files: dict[str, bytes] | None, *,
                     actor: str, idempotency_key: str | None = None, source: str = "api") -> Draft:
        configuration = models.validate(configuration)
        if theme is not None:
            try:
                theme = theme_mod.validate_manifest(theme)
            except theme_mod.ThemeError as exc:
                raise BrandError(422, exc.code, exc.message, pointer=f"/theme{exc.pointer or ''}")
        files = files or {}

        def change(conn):
            head, rev = self._active_row(conn)
            base = rev.id if rev is not None else NO_BASE
            listed, missing, _css = self._ingest(conn, configuration, files)
            state, validation, dig = self._evaluate(configuration, theme, listed, missing, base)
            now = time.time()
            draft_id = str(uuid.uuid4())
            conn.execute(drafts.insert().values(
                id=draft_id, deployment_id=self.deployment_id, base_revision=base,
                configuration_json=json.dumps(configuration), theme_json=json.dumps(theme),
                assets_json=json.dumps(listed), digest=dig, revision=1, state=state,
                validation_json=json.dumps(validation), creator=actor,
                validator_version=models.VALIDATOR_VERSION, source=source, created_at=now, updated_at=now))
            _audit(conn, actor, "branding.draft_created", subject=draft_id, detail={"digest": dig})
            return 201, draft_id

        outcome = idempotency.run(self.engine, deployment_id=self.deployment_id, actor_id=actor,
                                  route="POST /api/branding/drafts", key=idempotency_key,
                                  body={"configuration": configuration, "theme": theme,
                                        "files": {p: canonical.digest_bytes(b) for p, b in files.items()}},
                                  change=change, kind="branding.draft")
        return self.get_draft(outcome.body)

    def update_draft(self, draft_id: str, configuration: Any, theme: Any, files: dict[str, bytes] | None,
                     *, expected_revision: int, actor: str) -> Draft:
        configuration = models.validate(configuration)
        if theme is not None:
            try:
                theme = theme_mod.validate_manifest(theme)
            except theme_mod.ThemeError as exc:
                raise BrandError(422, exc.code, exc.message, pointer=f"/theme{exc.pointer or ''}")
        draft = self.get_draft(draft_id)
        files = dict(files or {})
        # Files already in the draft stay unless replaced.
        for a in draft.assets:
            if a.get("generated") or a["path"] in files:
                continue
            got = self.asset_bytes(a["digest"])
            if got is not None:
                files[a["path"]] = got[0]
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(drafts.c.revision).where(drafts.c.id == draft_id)).first()
            if row.revision != expected_revision:
                raise BrandError(409, "revision_conflict",
                                 "This draft changed since you loaded it. Reload it and try again.",
                                 current_revision=row.revision)
            head, rev = self._active_row(conn)
            base = rev.id if rev is not None else NO_BASE
            listed, missing, _css = self._ingest(conn, configuration, files)
            state, validation, dig = self._evaluate(configuration, theme, listed, missing, base)
            conn.execute(drafts.update().where(drafts.c.id == draft_id, drafts.c.revision == expected_revision)
                         .values(configuration_json=json.dumps(configuration), theme_json=json.dumps(theme),
                                 assets_json=json.dumps(listed), digest=dig, revision=expected_revision + 1,
                                 state=state, validation_json=json.dumps(validation), base_revision=base,
                                 updated_at=time.time()))
            _audit(conn, actor, "branding.draft_updated", subject=draft_id, detail={"digest": dig})
        return self.get_draft(draft_id)

    def put_asset(self, draft_id: str, path: str, data: bytes, *, actor: str) -> Draft:
        draft = self.get_draft(draft_id)
        if path not in models.referenced_paths(draft.configuration).values():
            raise BrandError(422, "validation_failed",
                             f"{path} is not referenced by this draft's branding.yaml.", pointer=path)
        return self.update_draft(draft_id, draft.configuration, draft.theme, {path: data},
                                 expected_revision=draft.revision, actor=actor)

    def revalidate(self, draft_id: str) -> Draft:
        """Re-check against the current base and validator (a base change makes
        the old digest, and any review of it, stale)."""
        draft = self.get_draft(draft_id)
        return self.update_draft(draft_id, draft.configuration, draft.theme, None,
                                 expected_revision=draft.revision, actor=draft.creator)

    # -- diff and preview -----------------------------------------------------------
    def diff(self, draft_id: str) -> dict:
        from ..chat.config import diff as doc_diff

        draft = self.get_draft(draft_id)
        with self.engine.connect() as conn:
            _head, rev = self._active_row(conn)
        old_conf = json.loads(rev.configuration_json) if rev else models.default()
        old_theme = json.loads(rev.theme_json) if rev else None
        old_assets = {a["key"]: a["digest"] for a in json.loads(rev.assets_json)} if rev else {}
        new_assets = {a["key"]: a["digest"] for a in draft.assets}
        asset_changes = [{"key": k, "old": old_assets.get(k), "new": new_assets.get(k)}
                         for k in sorted(set(old_assets) | set(new_assets))
                         if old_assets.get(k) != new_assets.get(k)]
        return {"draft_id": draft.id, "draft_revision": draft.revision, "digest": draft.digest,
                "base_revision": draft.base_revision,
                "configuration": doc_diff(old_conf, draft.configuration),
                "theme": doc_diff(old_theme or {}, draft.theme or {}),
                "assets": asset_changes}

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

    def preview(self, draft_id: str) -> dict:
        draft = self.get_draft(draft_id)
        out = self._resolved(draft.configuration, draft.theme, draft.assets,
                             lambda a: f"/api/branding/drafts/{draft.id}/assets/{a['digest']}")
        return {**out, "draft_id": draft.id, "draft_revision": draft.revision, "digest": draft.digest,
                "state": draft.state, "validation": draft.validation, "preview_only": True}

    def draft_asset(self, draft_id: str, digest: str) -> tuple[bytes, str] | None:
        draft = self.get_draft(draft_id)
        if digest not in {a["digest"] for a in draft.assets}:
            return None
        return self.asset_bytes(digest)

    # -- review and activation ------------------------------------------------------
    def review(self, draft_id: str, *, digest: str, expected_revision: int, decision: str, actor: str,
               reason: str | None = None, idempotency_key: str | None = None) -> dict:
        if decision not in ("approve", "reject"):
            raise BrandError(400, "invalid_request", "decision is approve or reject.")

        def change(conn):
            row = conn.execute(sa.select(drafts).where(drafts.c.id == draft_id,
                                                       drafts.c.deployment_id == self.deployment_id)).first()
            if row is None:
                raise BrandError(404, "not_found", "That branding draft was not found.")
            if row.revision != expected_revision or row.digest != digest:
                raise BrandError(409, "revision_conflict",
                                 "This draft changed since you reviewed it. Review the current version.",
                                 current_revision=row.revision, current_digest=row.digest)
            _h, rev = self._active_row(conn)
            base = rev.id if rev is not None else NO_BASE
            if row.base_revision != base:
                raise BrandError(409, "revision_conflict",
                                 "The active brand changed since this draft was made. Revalidate it and "
                                 "review again.", current_revision=row.revision)
            if row.validator_version != models.VALIDATOR_VERSION:
                raise BrandError(409, "revision_conflict",
                                 "Branding checks changed in this release. Revalidate the draft first.")
            if decision == "approve" and row.state not in ("valid", "reviewed"):
                validation = json.loads(row.validation_json)
                raise BrandError(422, "validation_failed",
                                 "This draft isn't valid yet: " + "; ".join(p["message"] for p in validation["problems"]),
                                 problems=validation["problems"])
            review_id = str(uuid.uuid4())
            now = time.time()
            conn.execute(reviews.insert().values(
                id=review_id, deployment_id=self.deployment_id, draft_id=draft_id, digest=digest,
                draft_revision=expected_revision, base_revision=row.base_revision,
                validator_version=row.validator_version, actor=actor, decision=decision, reason=reason,
                created_at=now))
            conn.execute(drafts.update().where(drafts.c.id == draft_id).values(
                state="reviewed" if decision == "approve" else "rejected", updated_at=now))
            _audit(conn, actor, f"branding.review_{decision}", subject=draft_id, detail={"digest": digest})
            return 201, {"review_id": review_id, "draft_id": draft_id, "digest": digest,
                         "draft_revision": expected_revision, "decision": decision, "actor": actor}

        return idempotency.run(self.engine, deployment_id=self.deployment_id, actor_id=actor,
                               route=f"POST /api/branding/drafts/{draft_id}/reviews", key=idempotency_key,
                               body={"digest": digest, "expected_revision": expected_revision,
                                     "decision": decision, "reason": reason},
                               change=change, kind="branding.review").body

    def activate(self, draft_id: str, *, review_id: str, expected_generation: int, actor: str,
                 idempotency_key: str | None = None, kind: str = "activate") -> dict:
        def change(conn):
            row = conn.execute(sa.select(drafts).where(drafts.c.id == draft_id,
                                                       drafts.c.deployment_id == self.deployment_id)).first()
            if row is None:
                raise BrandError(404, "not_found", "That branding draft was not found.")
            rv = conn.execute(sa.select(reviews).where(reviews.c.id == review_id,
                                                       reviews.c.draft_id == draft_id)).first()
            if rv is None or rv.decision != "approve":
                raise BrandError(409, "review_required", "Activate needs an approving review of this draft.")
            if (rv.digest != row.digest or rv.draft_revision != row.revision
                    or rv.validator_version != models.VALIDATOR_VERSION):
                raise BrandError(409, "revision_conflict",
                                 "The draft changed after it was reviewed. Review the current version.",
                                 current_revision=row.revision)
            head, cur = self._active_row(conn)
            generation = head.generation if head is not None else 0
            base = cur.id if cur is not None else NO_BASE
            if expected_generation != generation:
                raise BrandError(409, "revision_conflict",
                                 "Someone activated another brand since you loaded this. Reload.",
                                 current_revision=generation)
            if rv.base_revision != base:
                raise BrandError(409, "revision_conflict",
                                 "The active brand changed after this review. Revalidate and review again.",
                                 current_revision=generation)
            configuration = json.loads(row.configuration_json)
            assets = json.loads(row.assets_json)
            css_text = None
            for a in assets:
                if a["key"] == "decorative_css":
                    got = self.asset_bytes(a["digest"])
                    css_text = got[0].decode("utf-8") if got else None
            existing = conn.execute(sa.select(revisions.c.id).where(
                revisions.c.deployment_id == self.deployment_id, revisions.c.digest == row.digest)).first()
            now = time.time()
            if existing is None:
                rev_id = str(uuid.uuid4())
                conn.execute(revisions.insert().values(
                    id=rev_id, deployment_id=self.deployment_id, digest=row.digest,
                    configuration_json=row.configuration_json, theme_json=row.theme_json,
                    assets_json=row.assets_json, css_text=css_text,
                    validator_version=row.validator_version, created_at=now))
            else:
                rev_id = existing.id
            if head is None:
                conn.execute(active.insert().values(deployment_id=self.deployment_id, revision_id=rev_id,
                                                    generation=1))
            else:
                moved = conn.execute(active.update().where(
                    active.c.deployment_id == self.deployment_id, active.c.generation == generation)
                    .values(revision_id=rev_id, generation=generation + 1)).rowcount
                if moved != 1:
                    raise BrandError(409, "revision_conflict", "Another activation won. Reload.",
                                     current_revision=generation + 1)
            activation_id = str(uuid.uuid4())
            kind_ = "rollback" if (row.source or "").startswith("rollback:") else kind
            conn.execute(activations.insert().values(
                id=activation_id, deployment_id=self.deployment_id, from_revision=None if cur is None else cur.id,
                to_revision=rev_id, review_id=review_id, actor=actor, kind=kind_,
                generation=generation + 1, created_at=now))
            conn.execute(drafts.update().where(drafts.c.id == draft_id).values(state="activated", updated_at=now))
            _audit(conn, actor, "theme.activated", subject=rev_id,
                   detail={"activation_id": activation_id, "digest": row.digest, "generation": generation + 1,
                           "kind": kind_})
            return 200, {"activation_id": activation_id, "revision": rev_id, "generation": generation + 1,
                         "digest": row.digest}

        return idempotency.run(self.engine, deployment_id=self.deployment_id, actor_id=actor,
                               route=f"POST /api/branding/drafts/{draft_id}/activate", key=idempotency_key,
                               body={"review_id": review_id, "expected_revision": expected_generation},
                               change=change, kind="branding.activate").body

    def rollback(self, target_revision: str, *, actor: str, idempotency_key: str | None = None) -> Draft:
        with self.engine.connect() as conn:
            rev = conn.execute(sa.select(revisions).where(revisions.c.id == target_revision,
                                                          revisions.c.deployment_id == self.deployment_id)).first()
        if rev is None:
            raise BrandError(404, "not_found", "That brand revision was not found.")
        files: dict[str, bytes] = {}
        for a in json.loads(rev.assets_json):
            if a.get("generated"):
                continue
            got = self.asset_bytes(a["digest"])
            if got is None:
                raise BrandError(503, "unavailable", f"The stored file for {a['path']} is missing.")
            files[a["path"]] = got[0]
        return self.create_draft(json.loads(rev.configuration_json), json.loads(rev.theme_json), files,
                                 actor=actor, idempotency_key=idempotency_key, source=f"rollback:{rev.id}")

    def history(self, limit: int = 100) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(activations).where(activations.c.deployment_id == self.deployment_id)
                                .order_by(activations.c.created_at.desc()).limit(limit)).all()
            revs = {r.id: r for r in conn.execute(sa.select(revisions.c.id, revisions.c.digest,
                                                             revisions.c.configuration_json)
                                                   .where(revisions.c.deployment_id == self.deployment_id))}
        out = []
        for r in rows:
            target = revs.get(r.to_revision)
            name = json.loads(target.configuration_json)["identity"]["name"] if target else None
            out.append({"activation_id": r.id, "from_revision": r.from_revision, "to_revision": r.to_revision,
                        "review_id": r.review_id, "actor": r.actor, "kind": r.kind,
                        "generation": r.generation, "created_at": r.created_at,
                        "digest": target.digest if target else None, "name": name})
        return out

    def export(self, revision_id: str) -> bytes:
        with self.engine.connect() as conn:
            rev = conn.execute(sa.select(revisions).where(revisions.c.id == revision_id,
                                                          revisions.c.deployment_id == self.deployment_id)).first()
        if rev is None:
            raise BrandError(404, "not_found", "That brand revision was not found.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(models.FILE, yamlfile.dumps(json.loads(rev.configuration_json)))
            theme = json.loads(rev.theme_json)
            if theme:
                zf.writestr(models.THEME_FILE, json.dumps(theme, indent=2) + "\n")
            for a in json.loads(rev.assets_json):
                if a.get("generated"):
                    continue
                got = self.asset_bytes(a["digest"])
                if got:
                    zf.writestr(a["path"], got[0])
            zf.writestr("REVISION.json", json.dumps({"revision": rev.id, "digest": rev.digest,
                                                     "validator_version": rev.validator_version}, indent=2))
        return buf.getvalue()

    # -- public view --------------------------------------------------------------
    def public_id(self) -> str:
        return self.deployment_id

    def active_manifest(self) -> dict | None:
        with self.engine.connect() as conn:
            head, rev = self._active_row(conn)
        if head is None:
            return None
        configuration = json.loads(rev.configuration_json)
        assets = json.loads(rev.assets_json)
        base = f"/branding/assets/{self.public_id()}"
        out = self._resolved(configuration, json.loads(rev.theme_json), assets,
                             lambda a: f"{base}/{_hexdigest(a['digest'])}/{a['key']}.{a.get('extension', 'bin')}")
        return {**out, "revision": rev.id, "generation": head.generation}

    def public_asset(self, hexdigest: str) -> tuple[bytes, str] | None:
        """An asset of an activated revision (never a draft-only file)."""
        dig = f"sha256:{hexdigest}"
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(revisions.c.assets_json).where(
                revisions.c.deployment_id == self.deployment_id)).all()
        for (assets_json,) in rows:
            if any(a["digest"] == dig for a in json.loads(assets_json)):
                return self.asset_bytes(dig)
        return None

    def active_theme(self) -> tuple[str, dict] | None:
        with self.engine.connect() as conn:
            head, rev = self._active_row(conn)
        if head is None:
            return None
        theme = json.loads(rev.theme_json)
        return rev.id, (theme_mod.resolve(theme, check=False) if theme else theme_mod.resolve())

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
        for name, slot in ((logo, "logo_light"), (favicon, "favicon")):
            if name:
                rel = f"branding/{name}"
                configuration["assets"][slot] = rel
                files[rel] = (folder / name).read_bytes()
                report.append({"file": name, "disposition": "mapped", "to": f"assets.{slot}"})
        for p in sorted(folder.iterdir()):
            if not p.is_file() or p.name in (logo, favicon):
                continue
            if p.name.lower() == "custom.css":
                report.append({"file": p.name, "disposition": "dropped",
                               "reason": "Unrestricted CSS is no longer served. Move decoration to "
                                         "presentation.decorative_css (brand slots only)."})
            else:
                report.append({"file": p.name, "disposition": "not_used",
                               "reason": "Not a 2.0 brand slot. Reference it in branding.yaml to use it."})
        return configuration, files, report


def _audit(conn, actor: str, action: str, *, subject: str | None = None, detail: dict | None = None) -> None:
    from ..control import audit

    audit.write(conn, actor, action, subject=subject, detail=detail, surface="branding")


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
