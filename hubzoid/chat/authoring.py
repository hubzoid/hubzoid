"""Authoring the hub's own files from the Workspace (HUB-81, first part).

The agent's instructions (``AGENTS.md``, ``agents/<name>/AGENTS.md``), skills
(``skills/<name>/SKILL.md`` and their text files) and shared prompts
(``prompts/<id>.md``) stay ordinary hub files: Claude Code, Codex, the CLI and
the Workspace all edit the same bytes. A change made here is a **draft**:
validated with the same loaders the runtime uses, shown as a diff against the
file it started from, then **approved** by a hub manager for its exact digest
and **published** with one atomic write. Publishing refuses a stale base (the
file changed since the draft began) instead of rebasing, and every published
version is kept so it can be rolled back (as a new, reviewed change). Teammates
keep using the published file until then. Code (tools, scripts) is never
authored or activated here.

**Prompts** carry typed ``{{variables}}``. Rendering checks the values on the
server and returns the text for the composer; nothing is sent.

**OpenAPI actions**: an imported OpenAPI document is validated
(``openapi-spec-validator``), refused if it references anything outside itself,
and reduced to the operations a person selected. ``FastMCP.from_openapi``
converts exactly those into tools for review; activating them for an agent
belongs to the reviewed publication workflow and is not done here.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..control import canonical
from . import graph as g

metadata = sa.MetaData()
Err = g.GraphError
MAX_FILE = 256 * 1024
TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv")
VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]{0,40})\s*\}\}")

drafts = sa.Table(
    "hz_authoring_drafts", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("path", sa.String(512), nullable=False),
    sa.Column("base_sha256", sa.String(64)),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("digest", sa.String(80), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("author_id", sa.String(64), nullable=False),
    sa.Column("reviewer_id", sa.String(64)),
    sa.Column("note", sa.Text),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

published = sa.Table(
    "hz_authoring_versions", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("path", sa.String(512), nullable=False, index=True),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("previous_sha256", sa.String(64)),
    sa.Column("draft_id", sa.String(64)),
    sa.Column("published_by", sa.String(64), nullable=False),
    sa.Column("published_at", sa.Float, nullable=False),
)

personal_prompts = sa.Table(
    "hz_personal_prompts", metadata,
    sa.Column("hub_id", sa.String(255), primary_key=True),
    sa.Column("owner_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

TABLES = (drafts, published, personal_prompts)


def _now() -> float:
    return time.time()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PromptVariable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    label: str | None = None
    required: bool = True
    max_length: int = Field(2000, ge=1, le=20000)
    options: list[str] | None = None
    default: str | None = None


class PromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    variables: list[PromptVariable] = Field(default_factory=list)


def parse_prompt(text: str) -> tuple[PromptSpec, str]:
    from .. import frontmatter

    fm, body = frontmatter.split(text)
    spec = PromptSpec.model_validate(fm or {})
    declared = {v.name for v in spec.variables}
    used = set(VAR.findall(body))
    missing = used - declared
    if missing:
        raise ValueError(f"variables used but not declared: {', '.join(sorted(missing))}")
    return spec, body


def kind_of(path: str) -> str:
    parts = Path(path).parts
    if path == "AGENTS.md" or (len(parts) == 3 and parts[0] == "agents" and parts[2] == "AGENTS.md"):
        return "agent"
    if len(parts) >= 3 and parts[0] == "skills":
        return "skill"
    if len(parts) == 2 and parts[0] == "prompts" and path.endswith(".md"):
        return "prompt"
    raise Err(422, "path_not_authorable", "Only AGENTS.md, agents/<name>/AGENTS.md, skills/<name>/... and "
                                          "prompts/<id>.md can be edited here.")


def validate(path: str, content: str) -> list[dict]:
    """Problems with a candidate file, from the runtime's own loaders. [] when valid."""
    from .. import frontmatter
    from ..loaders.agents import AgentSpec
    from ..loaders.skills import SkillSpec

    problems: list[dict] = []
    if len(content.encode("utf-8")) > MAX_FILE:
        return [{"code": "too_large", "message": f"Files edited here are at most {MAX_FILE // 1024} KiB."}]
    kind = kind_of(path)
    try:
        if kind == "agent":
            fm, body = frontmatter.split(content)
            if fm:
                AgentSpec.model_validate({"name": fm.get("name") or "agent",
                                          "description": fm.get("description") or (body.strip()[:80] or "x"), **fm})
            if not body.strip():
                problems.append({"code": "empty_instructions", "message": "The instructions are empty."})
        elif kind == "skill" and path.endswith("SKILL.md"):
            fm, body = frontmatter.split(content)
            SkillSpec.model_validate(fm or {})
            if not body.strip():
                problems.append({"code": "empty_skill", "message": "The skill has no body."})
        elif kind == "skill" and not path.endswith(TEXT_SUFFIXES):
            problems.append({"code": "not_text", "message": "Skills can carry text files only (no code)."})
        elif kind == "prompt":
            parse_prompt(content)
    except ValidationError as exc:
        for e in exc.errors()[:10]:
            problems.append({"code": "invalid_frontmatter", "field": ".".join(str(x) for x in e["loc"]),
                             "message": e["msg"]})
    except ValueError as exc:
        problems.append({"code": "invalid", "message": str(exc)})
    return problems


class AuthoringService:
    def __init__(self, hub_dir: Path, engine, hub: str):
        self.hub_dir = Path(hub_dir)
        self.engine = engine
        self.hub = hub

    # -- files ---------------------------------------------------------------------------------
    def _path(self, rel: str) -> Path:
        rel = (rel or "").strip().lstrip("/")
        if not rel or ".." in Path(rel).parts or rel.startswith("."):
            raise Err(422, "path_not_authorable", "That path can't be edited here.")
        kind_of(rel)
        target = (self.hub_dir / rel)
        try:
            target.resolve().relative_to(self.hub_dir.resolve())
        except ValueError:
            raise Err(422, "path_not_authorable", "That path is outside the hub.")
        if target.is_symlink():
            raise Err(422, "path_not_authorable", "Linked files can't be edited here.")
        return target

    def read(self, rel: str) -> tuple[str | None, str | None]:
        p = self._path(rel)
        if not p.is_file():
            return None, None
        text = p.read_text(encoding="utf-8")
        return text, sha(text)

    def files(self) -> list[dict]:
        """The authorable files of the hub, with their current hash."""
        out = []
        candidates = [self.hub_dir / "AGENTS.md", *sorted((self.hub_dir / "agents").glob("*/AGENTS.md")),
                      *sorted(p for p in (self.hub_dir / "skills").rglob("*") if p.is_file()),
                      *sorted((self.hub_dir / "prompts").glob("*.md"))]
        for p in candidates:
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(self.hub_dir).as_posix()
            try:
                kind = kind_of(rel)
            except Err:
                continue
            if kind == "skill" and not rel.endswith(TEXT_SUFFIXES):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            out.append({"path": rel, "kind": kind, "sha256": sha(text), "bytes": len(text.encode())})
        return out

    # -- drafts --------------------------------------------------------------------------------
    def _draft(self, conn, did: str):
        d = conn.execute(sa.select(drafts).where(drafts.c.id == did, drafts.c.hub_id == self.hub)).first()
        if d is None:
            raise Err(404, "not_found", "That draft was not found.")
        return d

    def _draft_json(self, d) -> dict:
        current, cur_sha = self.read(d.path)
        diff = "".join(difflib.unified_diff((current or "").splitlines(True), d.content.splitlines(True),
                                            fromfile=f"a/{d.path}", tofile=f"b/{d.path}"))
        return {"id": d.id, "path": d.path, "kind": kind_of(d.path), "state": d.state, "revision": d.revision,
                "digest": d.digest, "base_sha256": d.base_sha256, "current_sha256": cur_sha,
                "stale": (cur_sha or None) != (d.base_sha256 or None), "content": d.content, "diff": diff,
                "problems": validate(d.path, d.content), "author_id": d.author_id, "reviewer_id": d.reviewer_id,
                "note": d.note, "updated_at": d.updated_at}

    def create_draft(self, actor, rel: str, content: str | None) -> dict:
        self._path(rel)
        current, cur_sha = self.read(rel)
        text = current if content is None else content
        if text is None:
            raise Err(422, "validation_failed", "A new file needs its content.")
        did = str(uuid.uuid4())
        t = _now()
        with self.engine.begin() as conn:
            conn.execute(drafts.insert().values(
                id=did, hub_id=self.hub, path=rel, base_sha256=cur_sha, content=text,
                digest=canonical.digest({"path": rel, "base": cur_sha, "content": text}), state="draft",
                author_id=actor.id, reviewer_id=None, note=None, revision=1, created_at=t, updated_at=t))
            return self._draft_json(self._draft(conn, did))

    def update_draft(self, actor, did: str, content: str, expected_revision: int) -> dict:
        with self.engine.begin() as conn:
            d = self._draft(conn, did)
            if d.author_id != actor.id:
                raise Err(403, "permission_denied", "Only the draft's author edits it.")
            if d.state not in ("draft", "rejected"):
                raise Err(409, "invalid_state", "This draft was already reviewed. Start a new one.")
            if d.revision != expected_revision:
                raise Err(409, "revision_conflict", "The draft changed. Reload it.", current_revision=d.revision)
            conn.execute(drafts.update().where(drafts.c.id == did).values(
                content=content, digest=canonical.digest({"path": d.path, "base": d.base_sha256, "content": content}),
                state="draft", reviewer_id=None, note=None, revision=d.revision + 1, updated_at=_now()))
            return self._draft_json(self._draft(conn, did))

    def get_draft(self, did: str) -> dict:
        with self.engine.connect() as conn:
            return self._draft_json(self._draft(conn, did))

    def list_drafts(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(drafts).where(drafts.c.hub_id == self.hub,
                                                        drafts.c.state.in_(("draft", "in_review", "approved", "rejected")))
                                .order_by(drafts.c.updated_at.desc()).limit(100)).all()
        return [self._draft_json(r) for r in rows]

    def submit(self, actor, did: str, expected_revision: int) -> dict:
        with self.engine.begin() as conn:
            d = self._draft(conn, did)
            if d.revision != expected_revision:
                raise Err(409, "revision_conflict", "The draft changed. Reload it.", current_revision=d.revision)
            problems = validate(d.path, d.content)
            if problems:
                raise Err(422, "invalid_draft", problems[0]["message"], problems=problems)
            conn.execute(drafts.update().where(drafts.c.id == did).values(state="in_review", updated_at=_now()))
            return self._draft_json(self._draft(conn, did))

    def review(self, actor, did: str, *, decision: str, digest: str, note: str | None = None) -> dict:
        """A hub manager approves or rejects the exact digest they reviewed."""
        if decision not in ("approve", "reject"):
            raise Err(422, "validation_failed", "decision is approve or reject.")
        with self.engine.begin() as conn:
            d = self._draft(conn, did)
            if d.state != "in_review":
                raise Err(409, "invalid_state", "Only a draft in review can be decided.")
            if d.digest != digest:
                raise Err(409, "review_superseded", "The draft changed after you reviewed it. Review it again.")
            conn.execute(drafts.update().where(drafts.c.id == did).values(
                state="approved" if decision == "approve" else "rejected", reviewer_id=actor.id,
                note=(note or "")[:2000] or None, updated_at=_now()))
            return self._draft_json(self._draft(conn, did))

    def publish(self, actor, did: str, digest: str) -> dict:
        """Write the approved bytes in one step, if the file is still what the
        draft started from. A stale base is refused, never rebased."""
        with self.engine.begin() as conn:
            d = self._draft(conn, did)
            if d.state != "approved" or d.digest != digest:
                raise Err(409, "not_approved", "Publish exactly the approved draft.")
            current, cur_sha = self.read(d.path)
            if (cur_sha or None) != (d.base_sha256 or None):
                raise Err(409, "stale_base", "The file changed since this draft began. Start a new draft from the "
                                             "current file.", current_sha256=cur_sha)
            target = self._path(d.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(d.content, encoding="utf-8")
            os.replace(tmp, target)
            new_sha = sha(d.content)
            vid = str(uuid.uuid4())
            if current is not None and conn.execute(sa.select(published.c.id).where(
                    published.c.hub_id == self.hub, published.c.path == d.path,
                    published.c.sha256 == cur_sha)).first() is None:
                # Keep the version this replaces (it was written outside the Workspace).
                conn.execute(published.insert().values(id=str(uuid.uuid4()), hub_id=self.hub, path=d.path,
                                                       sha256=cur_sha, content=current, previous_sha256=None,
                                                       draft_id=None, published_by="file", published_at=_now() - 0.001))
            conn.execute(published.insert().values(id=vid, hub_id=self.hub, path=d.path, sha256=new_sha,
                                                   content=d.content, previous_sha256=cur_sha, draft_id=did,
                                                   published_by=actor.id, published_at=_now()))
            conn.execute(drafts.update().where(drafts.c.id == did).values(state="published", updated_at=_now()))
            return {"path": d.path, "sha256": new_sha, "version_id": vid, "previous_sha256": cur_sha}

    def versions(self, rel: str) -> list[dict]:
        self._path(rel)
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(published).where(published.c.hub_id == self.hub, published.c.path == rel)
                                .order_by(published.c.published_at.desc())).all()
        _cur, cur_sha = self.read(rel)
        return [{"version_id": r.id, "sha256": r.sha256, "published_by": r.published_by,
                 "published_at": r.published_at, "current": r.sha256 == cur_sha} for r in rows]

    def rollback_draft(self, actor, rel: str, version_id: str) -> dict:
        """A new draft that restores an earlier published version (reviewed like any change)."""
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(published).where(published.c.id == version_id, published.c.hub_id == self.hub,
                                                        published.c.path == rel)).first()
        if r is None:
            raise Err(404, "not_found", "That version was not found.")
        out = self.create_draft(actor, rel, r.content)
        with self.engine.begin() as conn:
            conn.execute(drafts.update().where(drafts.c.id == out["id"]).values(note=f"Roll back to {r.sha256[:12]}"))
        return self.get_draft(out["id"])

    # -- prompts ---------------------------------------------------------------------------------
    def prompts(self, actor) -> list[dict]:
        out = []
        for p in sorted((self.hub_dir / "prompts").glob("*.md")):
            try:
                spec, _body = parse_prompt(p.read_text(encoding="utf-8"))
                out.append({"id": p.stem, "scope": "hub", **spec.model_dump()})
            except Exception as exc:  # noqa: BLE001 - an invalid file is listed as such
                out.append({"id": p.stem, "scope": "hub", "invalid": str(exc)[:200]})
        with self.engine.connect() as conn:
            for r in conn.execute(sa.select(personal_prompts).where(personal_prompts.c.hub_id == self.hub,
                                                                    personal_prompts.c.owner_id == actor.id)):
                try:
                    spec, _ = parse_prompt(r.content)
                    out.append({"id": r.id, "scope": "personal", "revision": r.revision, **spec.model_dump()})
                except Exception:  # noqa: BLE001
                    continue
        return out

    def put_personal_prompt(self, actor, pid: str, content: str, expected_revision) -> dict:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", pid or ""):
            raise Err(422, "validation_failed", "A prompt id is lowercase letters, digits and dashes.")
        try:
            parse_prompt(content)
        except (ValidationError, ValueError) as exc:
            raise Err(422, "invalid_prompt", str(exc).splitlines()[0])
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(personal_prompts).where(personal_prompts.c.hub_id == self.hub,
                                                                 personal_prompts.c.owner_id == actor.id,
                                                                 personal_prompts.c.id == pid)).first()
            want = None if expected_revision in (None, "absent") else int(expected_revision)
            if want != (row.revision if row else None):
                raise Err(409, "revision_conflict", "The prompt changed. Reload it.")
            if row is None:
                conn.execute(personal_prompts.insert().values(hub_id=self.hub, owner_id=actor.id, id=pid,
                                                              content=content, revision=1, updated_at=_now()))
            else:
                conn.execute(personal_prompts.update().where(personal_prompts.c.hub_id == self.hub,
                                                             personal_prompts.c.owner_id == actor.id,
                                                             personal_prompts.c.id == pid)
                             .values(content=content, revision=row.revision + 1, updated_at=_now()))
        return {"id": pid, "revision": (row.revision + 1) if row else 1}

    def render(self, actor, pid: str, scope: str, values: dict) -> dict:
        """The prompt's text with checked values, for the composer (never sent)."""
        if scope == "hub":
            p = self.hub_dir / "prompts" / f"{pid}.md"
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", pid or "") or not p.is_file():
                raise Err(404, "not_found", "That prompt was not found.")
            text, version = p.read_text(encoding="utf-8"), None
        else:
            with self.engine.connect() as conn:
                r = conn.execute(sa.select(personal_prompts).where(personal_prompts.c.hub_id == self.hub,
                                                                   personal_prompts.c.owner_id == actor.id,
                                                                   personal_prompts.c.id == pid)).first()
            if r is None:
                raise Err(404, "not_found", "That prompt was not found.")
            text, version = r.content, r.revision
        spec, body = parse_prompt(text)
        if not isinstance(values, dict):
            raise Err(422, "validation_failed", "values maps variable names to text.")
        unknown = set(values) - {v.name for v in spec.variables}
        if unknown:
            raise Err(422, "validation_failed", f"Unknown variable: {', '.join(sorted(unknown))}.")
        final: dict[str, str] = {}
        for v in spec.variables:
            got = values.get(v.name, v.default)
            if got is None or str(got).strip() == "":
                if v.required:
                    raise Err(422, "validation_failed", f"{v.label or v.name} is required.", fields={v.name: "required"})
                got = ""
            got = str(got)
            if len(got) > v.max_length:
                raise Err(422, "validation_failed", f"{v.label or v.name} is too long.", fields={v.name: "too long"})
            if v.options and got and got not in v.options:
                raise Err(422, "validation_failed", f"{v.label or v.name} is one of: {', '.join(v.options)}.")
            final[v.name] = got
        text_out = VAR.sub(lambda m: final.get(m.group(1), ""), body).strip()
        return {"id": pid, "scope": scope, "version": version or sha(text)[:12], "text": text_out}

    # -- OpenAPI actions -----------------------------------------------------------------------------
    @staticmethod
    def openapi_preview(spec: dict, selected: list[str] | None = None) -> dict:
        """Validate an OpenAPI document and list its operations; with
        ``selected``, convert exactly those into tools (FastMCP) for review."""
        from openapi_spec_validator import validate as validate_spec

        if not isinstance(spec, dict):
            raise Err(422, "invalid_openapi", "The OpenAPI document is a JSON or YAML object.")
        refs = [r for r in _refs(spec) if not r.startswith("#")]
        if refs:
            raise Err(422, "remote_reference", "The document refers to other files or addresses; import a "
                                               "self-contained document.", references=refs[:10])
        try:
            validate_spec(spec)
        except Exception as exc:  # noqa: BLE001 - the validator's first message
            raise Err(422, "invalid_openapi", f"Not a valid OpenAPI document: {str(exc).splitlines()[0][:300]}")
        ops = []
        for path, item in (spec.get("paths") or {}).items():
            for method, op in (item or {}).items():
                if method.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                    continue
                ops.append({"operation_id": op.get("operationId") or f"{method}_{path}", "method": method.upper(),
                            "path": path, "summary": op.get("summary") or "",
                            "writes": method.lower() != "get"})
        out = {"title": (spec.get("info") or {}).get("title"), "operations": ops, "tools": None,
               "digest": canonical.digest(spec)}
        if selected is not None:
            known = {o["operation_id"] for o in ops}
            bad = [s for s in selected if s not in known]
            if bad:
                raise Err(422, "unknown_operation", f"Not in the document: {', '.join(bad)}.")
            out["tools"] = _fastmcp_tools(spec, set(selected))
        return out


def _refs(node) -> list[str]:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out.append(v)
            else:
                out += _refs(v)
    elif isinstance(node, list):
        for v in node:
            out += _refs(v)
    return out


def _fastmcp_tools(spec: dict, selected: set[str]) -> list[dict]:
    """The tools FastMCP makes from the selected operations only (no calls are made)."""
    import asyncio

    import httpx
    from fastmcp import FastMCP
    from fastmcp.server.providers.openapi import MCPType, RouteMap

    def only_selected(route, mcp_type):
        return MCPType.TOOL if route.operation_id in selected else MCPType.EXCLUDE

    base = ((spec.get("servers") or [{}])[0].get("url") or "https://api.invalid")
    client = httpx.AsyncClient(base_url=base if base.startswith("https://") else "https://api.invalid")
    server = FastMCP.from_openapi(openapi_spec=spec, client=client, name="review",
                                  route_maps=[RouteMap(mcp_type=MCPType.EXCLUDE, pattern=r"^$")],
                                  route_map_fn=only_selected)

    async def listed():
        tools = await server.list_tools()
        await client.aclose()
        return [{"name": t.name, "description": (t.description or "")[:300],
                 "input_schema": json.loads(json.dumps(t.parameters))} for t in tools]

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(listed())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, listed()).result()


_services: dict[str, AuthoringService] = {}


def service_for(hub_dir: Path, engine, hub: str) -> AuthoringService:
    key = str(Path(hub_dir).resolve())
    svc = _services.get(key)
    if svc is None or svc.engine is not engine:
        svc = AuthoringService(hub_dir, engine, hub)
        _services[key] = svc
    return svc
