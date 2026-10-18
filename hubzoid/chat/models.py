"""Models, presets and side-by-side comparison in the chat (HUB-78, first part).

**Catalog.** The models people may pick come from the ``models`` namespace of
``chat/config.yaml`` (a label and the runtime's own model id per entry). With
no catalog configured, the runtime offers what it supports itself: Claude's
model aliases on the Claude runtime, the agent's own model elsewhere. A turn
names a catalog entry; the server checks it before the runtime sees it, and the
answer records both the model asked for and the one that answered.

**Presets** are named choices (a model, or models to compare, plus optional
parameters). Personal presets belong to one person and are revisioned; shared
presets are files in the hub (``presets/<id>.yaml``) and are read-only here.

**Comparison** (for people granted ``chat_model_compare``) answers the same
message with two or three models at once, each on its own branch, so the
person can read them side by side, keep one (select its branch), keep both, or
combine them (synthesis). Candidate answers may only read: their tools are
limited to ``comparison.candidate_tools``; a candidate never acts. Acting
candidates with reviewed actions come with request approvals, later.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import graph as g

metadata = sa.MetaData()
Err = g.GraphError
REF = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
READ_ONLY_TOOLS = ["current_time", "list_knowledge", "read_knowledge", "read_upload", "read_upload_full",
                   "search_sources", "list_skills", "read_skill"]
#: What the Claude runtime accepts by name (its own aliases).
CLAUDE_MODELS = [("sonnet", "Claude Sonnet"), ("opus", "Claude Opus"), ("haiku", "Claude Haiku")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogEntry(_Strict):
    label: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    parameters: dict = Field(default_factory=dict)


class ModelsSettings(_Strict):
    allow_choice: bool = True
    default_ref: str | None = None
    catalog: dict[str, CatalogEntry] = Field(default_factory=dict)

    @field_validator("catalog")
    @classmethod
    def _refs(cls, v):
        bad = [k for k in v if not REF.match(k)]
        if bad:
            raise ValueError(f"catalog ids are lowercase letters, digits and dashes: {', '.join(bad)}")
        return v


class ComparisonSettings(_Strict):
    enabled: bool = True
    max_candidates: int = Field(2, ge=2, le=3)
    candidate_tools: list[str] = Field(default_factory=lambda: list(READ_ONLY_TOOLS))


class Preset(_Strict):
    version: int = 1
    id: str
    name: str = Field(min_length=1, max_length=120)
    scope: str = "personal"
    agent_id: str | None = None
    model_ref: str | None = None
    candidate_refs: list[str] | None = None
    parameters: dict = Field(default_factory=dict)
    default: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not REF.match(v):
            raise ValueError("a preset id is lowercase letters, digits and dashes")
        return v


def register() -> None:
    from .config import namespaces, register_namespace

    for name, model in (("models", ModelsSettings), ("comparison", ComparisonSettings)):
        ns = namespaces().get(name)
        if ns is None or not ns.installed:
            register_namespace(name, model, owner="HUB-78")


register()

presets = sa.Table(
    "hz_chat_presets", metadata,
    sa.Column("hub_id", sa.String(255), primary_key=True),
    sa.Column("owner_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("body_json", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)
TABLES = (presets,)


class ModelService:
    def __init__(self, hub_dir: Path, engine, hub: str, runtime=None):
        self.hub_dir = Path(hub_dir)
        self.engine = engine
        self.hub = hub
        self.runtime = runtime

    def _doc(self) -> dict:
        from .config import service_for

        try:
            return service_for(self.hub_dir).active().document
        except Exception:  # noqa: BLE001
            return {"models": ModelsSettings().model_dump(), "comparison": ComparisonSettings().model_dump()}

    def runtime_kind(self) -> str:
        name = type(self.runtime).__name__.lower() if self.runtime is not None else ""
        return "claude" if "claude" in name else "codex" if "codex" in name else "openai" if name else "unknown"

    def catalog(self) -> list[dict]:
        """[{ref, label, model}] the person may pick; the first is the agent's own."""
        cfg = self._doc()["models"]
        out = [{"ref": "default", "label": "Agent default", "model": None}]
        if cfg["catalog"]:
            out += [{"ref": k, "label": v["label"], "model": v["model"]} for k, v in cfg["catalog"].items()]
        elif self.runtime_kind() == "claude":
            out += [{"ref": m, "label": label, "model": m} for m, label in CLAUDE_MODELS]
        return out

    def resolve(self, ref: str | None) -> dict | None:
        if not ref or ref == "default":
            return None
        return next((c for c in self.catalog() if c["ref"] == ref), None)

    def allowed(self, model: str, email: str = "") -> bool:
        """Whether a turn may run on ``model`` (a catalog model id)."""
        cfg = self._doc()["models"]
        return bool(cfg["allow_choice"]) and any(c["model"] == model for c in self.catalog() if c["model"])

    def describe(self, email: str) -> dict:
        from . import features

        doc = self._doc()
        feats = features.for_person(self.hub_dir, email)
        return {"catalog": self.catalog(), "default_ref": doc["models"]["default_ref"] or "default",
                "can_choose": bool(doc["models"]["allow_choice"]) and len(self.catalog()) > 1,
                "can_compare": bool(doc["comparison"]["enabled"] and feats.get("chat_model_compare")
                                    and len(self.catalog()) > 2),
                "max_candidates": doc["comparison"]["max_candidates"], "runtime": self.runtime_kind()}

    def compare_plan(self, email: str, refs: list) -> list[dict]:
        """The catalog entries to compare, after the grant and policy checks."""
        from . import features

        doc = self._doc()
        if not doc["comparison"]["enabled"]:
            raise Err(403, "feature_disabled", "Comparing models is turned off for this deployment.")
        if not features.for_person(self.hub_dir, email).get("chat_model_compare"):
            raise Err(403, "permission_denied", "Comparing models needs the chat_model_compare grant.")
        if not isinstance(refs, list) or not 2 <= len(refs) <= doc["comparison"]["max_candidates"] \
                or len(set(refs)) != len(refs):
            raise Err(422, "invalid_operation", f"Choose 2 to {doc['comparison']['max_candidates']} different models.")
        out = []
        for ref in refs:
            if ref == "default":
                out.append({"ref": "default", "label": "Agent default", "model": None})
                continue
            entry = self.resolve(ref)
            if entry is None:
                raise Err(422, "unknown_model", f"{ref} is not a model you can choose here.")
            out.append(entry)
        return out

    def candidate_tools(self) -> list[str]:
        return list(self._doc()["comparison"]["candidate_tools"])

    # -- presets ---------------------------------------------------------------------------------
    def shared_presets(self) -> list[dict]:
        from ..control import yamlfile

        folder = self.hub_dir / "presets"
        out = []
        if folder.is_dir():
            for p in sorted(folder.glob("*.yaml")):
                try:
                    body = Preset.model_validate(yamlfile.load(p).data).model_dump()
                except Exception:  # noqa: BLE001 - an invalid file is listed as such
                    out.append({"id": p.stem, "scope": "hub", "invalid": True, "file": f"presets/{p.name}"})
                    continue
                out.append({**body, "scope": "hub", "revision": None, "file": f"presets/{p.name}"})
        return out

    def list_presets(self, actor) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(presets).where(presets.c.hub_id == self.hub, presets.c.owner_id == actor.id)
                                .order_by(presets.c.id)).all()
        return [{**json.loads(r.body_json), "scope": "personal", "revision": r.revision} for r in rows] + \
            self.shared_presets()

    def put_preset(self, actor, preset_id: str, body: dict, expected_revision) -> dict:
        data = {k: v for k, v in body.items() if k != "expected_revision"}
        data.update(id=preset_id, scope="personal")
        try:
            p = Preset.model_validate(data)
        except Exception as exc:  # noqa: BLE001
            first = (exc.errors() if hasattr(exc, "errors") else [{}])[0]
            raise Err(422, "invalid_config", f"{'.'.join(str(x) for x in first.get('loc', ()))}: {first.get('msg')}")
        if (p.model_ref is None) == (not p.candidate_refs):
            raise Err(422, "invalid_config", "A preset has either model_ref or candidate_refs.")
        refs = [p.model_ref] if p.model_ref else list(p.candidate_refs or [])
        known = {c["ref"] for c in self.catalog()}
        if any(r not in known for r in refs):
            raise Err(422, "unknown_model", "A preset can only name models in this deployment's catalog.")
        t = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(presets).where(presets.c.hub_id == self.hub, presets.c.owner_id == actor.id,
                                                        presets.c.id == preset_id)).first()
            current = row.revision if row else None
            want = None if expected_revision in ("absent", None) else int(expected_revision)
            if want != current:
                raise Err(409, "revision_conflict", "The preset changed. Reload it.",
                          current_revision=current if current is not None else "absent")
            if p.default:  # one default per person
                for other in conn.execute(sa.select(presets).where(presets.c.hub_id == self.hub,
                                                                   presets.c.owner_id == actor.id,
                                                                   presets.c.id != preset_id)):
                    b = json.loads(other.body_json)
                    if b.get("default"):
                        b["default"] = False
                        conn.execute(presets.update().where(presets.c.hub_id == self.hub,
                                                            presets.c.owner_id == actor.id, presets.c.id == other.id)
                                     .values(body_json=json.dumps(b), revision=other.revision + 1, updated_at=t))
            body_json = json.dumps(p.model_dump())
            if row is None:
                conn.execute(presets.insert().values(hub_id=self.hub, owner_id=actor.id, id=preset_id,
                                                     body_json=body_json, revision=1, updated_at=t))
            else:
                conn.execute(presets.update().where(presets.c.hub_id == self.hub, presets.c.owner_id == actor.id,
                                                    presets.c.id == preset_id)
                             .values(body_json=body_json, revision=row.revision + 1, updated_at=t))
        return {**p.model_dump(), "scope": "personal", "revision": (current or 0) + 1}

    def delete_preset(self, actor, preset_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(presets.delete().where(presets.c.hub_id == self.hub, presets.c.owner_id == actor.id,
                                                presets.c.id == preset_id))


_services: dict[str, ModelService] = {}


def service_for(hub_dir: Path, engine, hub: str, runtime=None) -> ModelService:
    key = str(Path(hub_dir).resolve())
    svc = _services.get(key)
    if svc is None or svc.engine is not engine:
        svc = ModelService(hub_dir, engine, hub, runtime)
        _services[key] = svc
    elif runtime is not None:
        svc.runtime = runtime
    return svc
