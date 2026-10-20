"""Authoring the hub's own files from the Workspace (HUB-81, first part).

The agent's instructions (``AGENTS.md``, ``agents/<name>/AGENTS.md``), skills
(``skills/<name>/SKILL.md`` and their text files, or a flat ``skills/<name>.md``;
the folder may be spelt as the loaders accept) and shared prompts
(``prompts/<id>.md``) stay ordinary hub files: Claude Code, Codex, the CLI and
the Workspace all edit the same bytes. A change made in the Workspace is a
**hub change** (``hubzoid.hub_changes``): proposed, approved by a hub manager
and committed to Git with the proposer and approver in its trailers. History is
the file's Git history (``versions``); a rollback proposes an earlier text as a
new change. Code (tools, scripts) is never authored here.

**Prompts** carry typed ``{{variables}}``. Rendering checks the values on the
server and returns the text for the composer; nothing is sent.

**OpenAPI actions**: an imported OpenAPI document is validated
(``openapi-spec-validator``), refused if it references anything outside itself,
and reduced to the operations a person selected. ``FastMCP.from_openapi``
converts exactly those into tools for review; activating them for an agent
belongs to the reviewed publication workflow and is not done here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..control import canonical
from . import graph as g

log = logging.getLogger(__name__)

metadata = sa.MetaData()
Err = g.GraphError
MAX_FILE = 256 * 1024
TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv")
VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]{0,40})\s*\}\}")

personal_prompts = sa.Table(
    "hz_personal_prompts", metadata,
    sa.Column("hub_id", sa.String(255), primary_key=True),
    sa.Column("owner_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
)

TABLES = (personal_prompts,)
#: one publication at a time per (hub, file) in this process
_PATH_LOCKS: dict = {}


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


def _bucket(name: str) -> str:
    """``agents`` or ``skills`` for any spelling the loaders accept (``Skill``,
    ``agent``…, ``hubzoid._fs``); other names as they are."""
    from .._fs import _ALIASES

    low = name.lower()
    return next((b for b in ("agents", "skills") if low in _ALIASES[b]), name)


def _flat_skill(parts: tuple[str, ...]) -> bool:
    """``skills/<name>.md``: the loader's flat skill shape."""
    return len(parts) == 2 and parts[1].lower().endswith(".md")


def kind_of(path: str) -> str:
    parts = Path(path).parts
    first = _bucket(parts[0]) if parts else ""
    if path == "AGENTS.md" or (len(parts) == 3 and first == "agents" and parts[2] == "AGENTS.md"):
        return "agent"
    if first == "skills" and (len(parts) >= 3 or _flat_skill(parts)):
        return "skill"
    if len(parts) == 2 and parts[0] == "prompts" and path.endswith(".md"):
        return "prompt"
    raise Err(422, "path_not_authorable", "Only AGENTS.md, agents/<name>/AGENTS.md, skills/<name>/..., skills/<name>.md and "
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
        elif kind == "skill" and (Path(path).name.lower() == "skill.md" or _flat_skill(Path(path).parts)):
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
        problems.append({"code": "invalid", "message": _plain(exc)})
    return problems


def _plain(exc: ValueError) -> str:
    """A loader error in words a person can act on (no parser internals)."""
    import yaml

    cause = exc.__cause__
    if isinstance(cause, yaml.YAMLError):
        mark = getattr(cause, "problem_mark", None)
        where = f" near line {mark.line + 2}" if mark is not None else ""  # +1 for the opening ---
        return (f"The settings block between the --- lines at the top isn't valid{where}. "
                "Check for a missing colon, or quote a value that contains a colon.")
    if "frontmatter must be a YAML mapping" in str(exc):
        return "The settings block between the --- lines at the top needs `key: value` lines."
    return str(exc)


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
        parts = Path(rel).parts
        if _bucket(parts[0]) in ("agents", "skills"):
            # A new skill goes into the folder the hub already has (Skill/, skills/…).
            from .._fs import resolve_bucket

            existing = resolve_bucket(self.hub_dir, _bucket(parts[0]))
            if existing is not None and existing.name != parts[0]:
                rel = Path(existing.name, *parts[1:]).as_posix()
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
        from .._fs import resolve_bucket

        agents_dir = resolve_bucket(self.hub_dir, "agents") or self.hub_dir / "agents"
        skills_dir = resolve_bucket(self.hub_dir, "skills") or self.hub_dir / "skills"
        candidates = [self.hub_dir / "AGENTS.md", *sorted(agents_dir.glob("*/AGENTS.md")),
                      *sorted(p for p in skills_dir.rglob("*") if p.is_file()),
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

    def versions(self, rel: str) -> list[dict]:
        """A file's history: its commits in the hub's Git repository (none
        when the hub isn't in one)."""
        import subprocess

        self._path(rel)
        try:
            out = subprocess.run(["git", "-C", str(self.hub_dir), "log", "-n", "30",
                                  "--format=%H%x1f%an%x1f%at%x1f%s", "--", rel],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        items = []
        for line in out.stdout.splitlines() if out.returncode == 0 else []:
            sha_, author, at, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
            items.append({"commit": sha_, "author": author, "at": float(at or 0), "subject": subject})
        return items

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
