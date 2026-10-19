"""``mcp-apps.yaml`` and ``cards/*.yaml``: which tools show cards.

Both files are optional; without ``mcp-apps.yaml`` no tool shows a card.

```yaml
# mcp-apps.yaml
version: 1
enabled: true
builtin_cards: [table, report, progress, approval]
card_manifests: [cards/order-summary.yaml]   # omitted: every cards/*.yaml
limits:                  # lower only; these defaults are the ceilings
  html_bytes: 1048576
  result_bytes: 262144
  rows_per_page: 100
  calls_per_minute: 60
  concurrent_calls: 4
  resource_timeout_seconds: 10   # 1 to 30
```

```yaml
# cards/order-summary.yaml
version: 1
id: order-summary
title: Synthetic order summary
binding:
  tool: order_summary
  output_schema: schemas/order-summary.json   # optional JSON Schema for the result
resource:
  builtin: table             # or: file: cards/dist/summary.html + sha256: <hex>
visibility: [model, app]
allowed_tools: [order_summary]   # what the card may call (read tools only)
```

Unknown keys fail. Every error names the file, a JSON Pointer and the line.
A change takes effect when it is **applied** (``hubzoid apps apply``): the
checked configuration becomes the next revision, compared and set against the
revision it was based on, and the card HTML is stored by its SHA-256.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..control import canonical, yamlfile
from . import kinds

FILE = "mcp-apps.yaml"
MIME = "text/html;profile=mcp-app"
_ID = r"^[a-z0-9][a-z0-9-]{0,62}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(_Strict):
    html_bytes: int = Field(1_048_576, ge=1024, le=1_048_576)
    result_bytes: int = Field(262_144, ge=1024, le=262_144)
    rows_per_page: int = Field(100, ge=1, le=100)
    fields_per_form: int = Field(32, ge=1, le=32)
    calls_per_minute: int = Field(60, ge=1, le=60)
    concurrent_calls: int = Field(4, ge=1, le=4)
    resource_timeout_seconds: int = Field(10, ge=1, le=30)


class AppsDoc(_Strict):
    version: Literal[1]
    enabled: bool = True
    builtin_cards: list[str] = Field(default_factory=lambda: list(kinds.BUILTIN))
    card_manifests: list[str] | None = None
    limits: Limits = Field(default_factory=Limits)


class Binding(_Strict):
    tool: str = Field(min_length=1, max_length=255)
    output_schema: str | None = None


class ResourceDoc(_Strict):
    builtin: str | None = None
    file: str | None = None
    sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("sha256", mode="before")
    @classmethod
    def _quoted(cls, v):
        if v is not None and not isinstance(v, str):
            raise ValueError('must be quoted text: sha256: "<64 hex characters>"')
        return v


class CardDoc(_Strict):
    version: Literal[1]
    id: str = Field(pattern=_ID)
    title: str = Field(min_length=1, max_length=200)
    server_id: str = "hub"
    binding: Binding
    resource: ResourceDoc
    visibility: list[Literal["model", "app"]] = Field(default_factory=lambda: ["model", "app"], min_length=1)
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_resources: list[str] = Field(default_factory=list)


@dataclass
class Problem:
    code: str
    message: str
    file: str | None = None
    pointer: str | None = None
    line: int | None = None

    def dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Card:
    id: str
    title: str
    kind: str                  # a built-in kind, or "custom"
    tool: str
    server_id: str
    schema: dict | None
    visibility: list[str]
    allowed_tools: list[str]
    sha256: str
    html: str

    def uri(self, hub: str) -> str:
        return f"ui://{hub}/{self.id}/{self.sha256}/index.html"

    def public(self, hub: str) -> dict:
        return {"id": self.id, "title": self.title, "kind": self.kind, "tool": self.tool,
                "server_id": self.server_id, "visibility": self.visibility,
                "allowed_tools": self.allowed_tools, "resource_sha256": self.sha256,
                "resource_uri": self.uri(hub), "output_schema": self.schema is not None}


@dataclass
class Resolved:
    """A checked configuration: what ``apply`` stores as a revision."""

    present: bool
    enabled: bool = False
    limits: Limits = field(default_factory=Limits)
    cards: dict[str, Card] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems

    def stored(self) -> dict:
        """The canonical form kept with a revision (HTML is stored by hash)."""
        return {"enabled": self.enabled, "limits": self.limits.model_dump(),
                "cards": [{"id": c.id, "title": c.title, "kind": c.kind, "tool": c.tool,
                           "server_id": c.server_id, "schema": c.schema, "visibility": c.visibility,
                           "allowed_tools": c.allowed_tools, "sha256": c.sha256}
                          for c in sorted(self.cards.values(), key=lambda c: c.id)]}

    def digest(self) -> str:
        return canonical.digest(self.stored())


def builtin_html(kind: str) -> str:
    """A built-in card's HTML: its page with the shared client script inlined."""
    base = Path(__file__).parent / "builtin"
    page = (base / f"{kind}.html").read_text(encoding="utf-8")
    shared = (base / "card.js").read_text(encoding="utf-8")
    style = (base / "card.css").read_text(encoding="utf-8")
    return page.replace("/*HZ_CARD_CSS*/", style).replace("/*HZ_CARD_JS*/", shared)


def sha256_text(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def _inside(hub: Path, rel: str) -> Path | None:
    """``rel`` as a path inside the hub, or None when it leaves it (also through a link)."""
    if not rel or Path(rel).is_absolute():
        return None
    target = (hub / rel)
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to(hub.resolve()):
        return None
    return resolved


def _pydantic(exc: ValidationError, loaded: yamlfile.Loaded, rel: str) -> list[Problem]:
    out = []
    for err in exc.errors():
        pointer = "".join(f"/{p}" for p in err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        if err.get("type") == "extra_forbidden":
            msg = "is not a known key"
        out.append(Problem("config_invalid", f"{pointer or '/'} {msg}", rel, pointer or "/",
                           loaded.line_of(pointer)))
    return out


def load(hub_dir: Path, *, tool_names: set[str] | None = None, apps_doc: dict | None = None) -> Resolved:
    """Read and check the files. ``tool_names``: the hub's tools, to check the
    bindings against (None skips that check, for a quick syntax pass).
    ``apps_doc``: a candidate for ``mcp-apps.yaml`` checked in its place
    (the file is not read or written)."""
    hub = Path(hub_dir).resolve()
    path = hub / FILE
    if apps_doc is None and not path.is_file():
        return Resolved(present=False)
    res = Resolved(present=True, sources=[FILE])
    if apps_doc is not None:
        loaded = yamlfile.Loaded(dict(apps_doc), {}, FILE)
    else:
        try:
            loaded = yamlfile.load(path)
        except yamlfile.YamlError as exc:
            res.problems.append(Problem("config_invalid", exc.message, FILE, None, exc.line))
            return res
    try:
        doc = AppsDoc.model_validate(loaded.data)
    except ValidationError as exc:
        res.problems += _pydantic(exc, loaded, FILE)
        return res
    res.enabled, res.limits = doc.enabled, doc.limits
    for i, kind in enumerate(doc.builtin_cards):
        if kind in kinds.UNAVAILABLE:
            res.problems.append(Problem("card_unavailable", kinds.UNAVAILABLE[kind], FILE,
                                        f"/builtin_cards/{i}", loaded.line_of(f"/builtin_cards/{i}")))
        elif kind not in kinds.BUILTIN:
            res.problems.append(Problem("config_invalid", f"{kind!r} is not a built-in card.", FILE,
                                        f"/builtin_cards/{i}", loaded.line_of(f"/builtin_cards/{i}")))
    if doc.card_manifests is None:
        from .._fs import resolve_bucket

        folder = resolve_bucket(hub, "cards")
        manifests = sorted(str(p.relative_to(hub)) for p in folder.glob("*.yaml")) if folder else []
    else:
        manifests = list(doc.card_manifests)
    tools_seen: dict[str, str] = {}
    for i, rel in enumerate(manifests):
        card = _card(hub, rel, doc, res, tool_names)
        if card is None:
            continue
        if card.id in res.cards:
            res.problems.append(Problem("config_invalid", f"Two cards use the id {card.id!r}.", rel, "/id"))
            continue
        if card.tool in tools_seen:
            res.problems.append(Problem("config_invalid", f"The tool {card.tool!r} already has the card "
                                        f"{tools_seen[card.tool]!r}; a tool shows one card.", rel, "/binding/tool"))
            continue
        tools_seen[card.tool] = card.id
        res.cards[card.id] = card
        res.sources.append(rel)
    return res


def _card(hub: Path, rel: str, doc: AppsDoc, res: Resolved, tool_names: set[str] | None) -> Card | None:
    path = _inside(hub, rel)
    if path is None:
        res.problems.append(Problem("config_invalid", "The card file is missing or outside the hub.", rel))
        return None
    try:
        loaded = yamlfile.load(path)
        card = CardDoc.model_validate(loaded.data)
    except yamlfile.YamlError as exc:
        res.problems.append(Problem("config_invalid", exc.message, rel, None, exc.line))
        return None
    except ValidationError as exc:
        res.problems += _pydantic(exc, loaded, rel)
        return None
    line = loaded.line_of
    before = len(res.problems)
    if card.server_id != "hub":
        res.problems.append(Problem("config_invalid", "Only the hub's own tools can show cards here "
                                    "(server_id: hub).", rel, "/server_id", line("/server_id")))
    if tool_names is not None and card.binding.tool not in tool_names:
        res.problems.append(Problem("tool_unknown", f"The hub has no tool {card.binding.tool!r}.", rel,
                                    "/binding/tool", line("/binding/tool")))
    for i, name in enumerate(card.allowed_tools):
        if tool_names is not None and name not in tool_names:
            res.problems.append(Problem("tool_unknown", f"The hub has no tool {name!r}.", rel,
                                        f"/allowed_tools/{i}", line(f"/allowed_tools/{i}")))
    if card.allowed_resources:
        res.problems.append(Problem("config_invalid", "Cards read only their own resource here; "
                                    "leave allowed_resources empty.", rel, "/allowed_resources",
                                    line("/allowed_resources")))
    schema = None
    if card.binding.output_schema:
        schema = _schema(hub, card.binding.output_schema, rel, res, line("/binding/output_schema"))
    r = card.resource
    kind, html = "custom", ""
    if bool(r.builtin) == bool(r.file):
        res.problems.append(Problem("config_invalid", "Give exactly one of resource.builtin and resource.file.",
                                    rel, "/resource", line("/resource")))
    elif r.builtin:
        if r.sha256:
            res.problems.append(Problem("config_invalid", "A built-in card has no sha256.", rel,
                                        "/resource/sha256", line("/resource/sha256")))
        if r.builtin in kinds.UNAVAILABLE:
            res.problems.append(Problem("card_unavailable", kinds.UNAVAILABLE[r.builtin], rel,
                                        "/resource/builtin", line("/resource/builtin")))
        elif r.builtin not in doc.builtin_cards or r.builtin not in kinds.BUILTIN:
            res.problems.append(Problem("config_invalid", f"{r.builtin!r} is not an enabled built-in card.",
                                        rel, "/resource/builtin", line("/resource/builtin")))
        else:
            kind, html = r.builtin, builtin_html(r.builtin)
    else:
        html = _bundle(hub, r, rel, res, line, doc.limits.html_bytes) or ""
    if len(res.problems) > before:
        return None
    return Card(id=card.id, title=card.title, kind=kind, tool=card.binding.tool, server_id=card.server_id,
                schema=schema, visibility=list(dict.fromkeys(card.visibility)),
                allowed_tools=list(dict.fromkeys(card.allowed_tools)), sha256=sha256_text(html), html=html)


def _schema(hub: Path, rel_schema: str, rel: str, res: Resolved, line: int | None) -> dict | None:
    path = _inside(hub, rel_schema)
    if path is None or not path.is_file():
        res.problems.append(Problem("output_schema_mismatch", "The output schema file is missing or outside "
                                    "the hub.", rel, "/binding/output_schema", line))
        return None
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001 - any parse or schema error is the builder's to fix
        res.problems.append(Problem("output_schema_mismatch", f"The output schema is not a valid JSON "
                                    f"Schema: {str(exc).splitlines()[0][:200]}", rel_schema))
        return None
    if not isinstance(schema, dict) or schema.get("type") != "object":
        res.problems.append(Problem("output_schema_mismatch", "The output schema must describe a JSON object "
                                    "(\"type\": \"object\").", rel_schema))
        return None
    return schema


def _bundle(hub: Path, r: ResourceDoc, rel: str, res: Resolved, line, limit: int) -> str | None:
    path = _inside(hub, r.file or "")
    if path is None or not path.is_file():
        res.problems.append(Problem("resource_unsafe", "The card HTML is missing or outside the hub.",
                                    rel, "/resource/file", line("/resource/file")))
        return None
    data = path.read_bytes()
    if len(data) > limit:
        res.problems.append(Problem("resource_unsafe", f"The card HTML is larger than {limit} bytes.",
                                    rel, "/resource/file", line("/resource/file")))
        return None
    digest = hashlib.sha256(data).hexdigest()
    if not r.sha256:
        res.problems.append(Problem("resource_hash_mismatch", f"Pin the reviewed file: sha256: {digest}",
                                    rel, "/resource/sha256", line("/resource")))
        return None
    if digest != r.sha256:
        res.problems.append(Problem("resource_hash_mismatch", f"The file changed since it was reviewed "
                                    f"(now {digest}). Review the diff, then pin the new sha256.",
                                    rel, "/resource/sha256", line("/resource/sha256")))
        return None
    try:
        html = data.decode("utf-8")
    except UnicodeDecodeError:
        res.problems.append(Problem("resource_unsafe", "The card HTML is not UTF-8.", rel, "/resource/file"))
        return None
    head = html.lstrip()[:15].lower()
    if not (head.startswith("<!doctype html") or head.startswith("<html")):
        res.problems.append(Problem("resource_unsafe", "The card must be one standalone HTML document.",
                                    rel, "/resource/file"))
        return None
    return html


def from_stored(stored: dict, html_for) -> Resolved:
    """A stored revision back as a ``Resolved`` (``html_for(sha256)`` reads HTML)."""
    res = Resolved(present=True, enabled=bool(stored.get("enabled")),
                   limits=Limits.model_validate(stored.get("limits") or {}))
    for c in stored.get("cards") or []:
        res.cards[c["id"]] = Card(id=c["id"], title=c["title"], kind=c["kind"], tool=c["tool"],
                                  server_id=c["server_id"], schema=c.get("schema"),
                                  visibility=list(c.get("visibility") or []),
                                  allowed_tools=list(c.get("allowed_tools") or []), sha256=c["sha256"],
                                  html=html_for(c["sha256"]) or "")
    return res


def set_pointer(hub_dir: Path, pointer: str, value: Any) -> str:
    """Set one value in ``mcp-apps.yaml`` by JSON Pointer (whole-value
    replacement; arrays are not appended). Returns the file written."""
    hub = Path(hub_dir)
    path = hub / FILE
    data = yamlfile.load(path).data if path.is_file() else {"version": 1}
    if not pointer.startswith("/") or pointer == "/":
        raise ValueError("Use a JSON Pointer such as /enabled.")
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
    node = data
    for p in parts[:-1]:
        nxt = node.get(p) if isinstance(node, dict) else None
        if nxt is None:
            nxt = {}
            node[p] = nxt
        if not isinstance(nxt, dict):
            raise ValueError(f"{pointer} goes through a value that is not a mapping.")
        node = nxt
    node[parts[-1]] = value
    AppsDoc.model_validate(data)
    path.write_text(yamlfile.dumps(data), encoding="utf-8")
    return FILE
