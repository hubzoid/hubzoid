"""Station definitions: isolated agents declared in `agents/<name>/AGENTS.md`.

A station is a sub-agent with `isolated: true`. It gets only its own
instructions and exactly the tools it lists, and returns a result checked
against its `response_schema`. Nothing from the main agent, its conversation,
skills or knowledge reaches it unless passed in the call or through a listed
tool. No runtime SDK is imported here.

Each definition has a digest over everything that decides its behaviour, so a
call pins the exact definition it ran and a file change never alters work in
progress.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..work_items.errors import WorkItemError

ENGINES = ("claude-local", "codex-local")
INPUT_MODES = ("forward", "never")
#: Tools stations get from Hubzoid itself, by name, when they list them.
STATION_TOOLS = ("save_handoff", "read_handoff")
MAX_SCHEMA_BYTES = 65536


@dataclass(frozen=True)
class Station:
    name: str
    description: str
    instructions: str
    engine: str
    model: str | None
    tools: tuple[str, ...]
    schema: dict | None
    input: str
    sandbox: str | None
    timeout_seconds: int | None
    source: str
    digest: str = ""
    extra: dict = field(default_factory=dict)

    def model_setting(self) -> str:
        """The model string the runtime takes: `claude-local/sonnet`, `codex-local`."""
        return f"{self.engine}/{self.model}" if self.model else self.engine

    def to_json(self) -> dict:
        return {"name": self.name, "description": self.description, "instructions": self.instructions,
                "engine": self.engine, "model": self.model, "tools": list(self.tools),
                "schema": self.schema, "input": self.input, "sandbox": self.sandbox,
                "timeout_seconds": self.timeout_seconds, "source": self.source, "digest": self.digest}

    @classmethod
    def from_json(cls, data: dict) -> "Station":
        return cls(name=data["name"], description=data["description"], instructions=data["instructions"],
                   engine=data["engine"], model=data.get("model"), tools=tuple(data.get("tools") or ()),
                   schema=data.get("schema"), input=data.get("input", "forward"), sandbox=data.get("sandbox"),
                   timeout_seconds=data.get("timeout_seconds"), source=data.get("source", ""),
                   digest=data.get("digest", ""))


def _digest(station: Station) -> str:
    body = {k: v for k, v in station.to_json().items() if k not in ("digest", "source")}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _hub_engine(hub_dir: Path) -> tuple[str, str | None]:
    """The hub's own runtime, as (engine, model)."""
    from .. import settings as settingslib
    from ..runtime import _resolve_model_id

    model_id = _resolve_model_id(hub_dir, settingslib.load(hub_dir))
    for engine in ENGINES:
        if model_id == engine or model_id.startswith(engine + "/"):
            return engine, model_id.partition("/")[2] or None
    return model_id, None


def _fail(source, message, field_name=None):
    raise WorkItemError("invalid_station_definition", f"{source}: {message}", status=422,
                        file=source, field=field_name)


def load_all(hub_dir) -> dict[str, Station]:
    """Every station in the hub, by name. Invalid ones raise."""
    from ..loaders import agents as agents_loader

    hub_dir = Path(hub_dir)
    out: dict[str, Station] = {}
    for loaded in agents_loader.load_subagents(hub_dir):
        if not loaded.spec.isolated:
            continue
        out[loaded.spec.name] = _build(hub_dir, loaded)
    return out


def load(hub_dir, name: str) -> Station:
    stations = load_all(hub_dir)
    if name not in stations:
        raise WorkItemError("unknown_station",
                            f"There is no isolated agent {name!r}. Stations: "
                            f"{', '.join(sorted(stations)) or 'none'}.", status=404)
    return stations[name]


def _build(hub_dir: Path, loaded) -> Station:
    spec = loaded.spec
    source = str(loaded.source_path.relative_to(hub_dir))
    folder = loaded.source_path.parent
    engine = spec.engine
    model = spec.model
    if engine is None:
        if model and any(model == e or model.startswith(e + "/") for e in ENGINES):
            engine, model = model.partition("/")[0], model.partition("/")[2] or None
        else:
            engine, default_model = _hub_engine(hub_dir)
            model = model or default_model
    if engine not in ENGINES:
        _fail(source, f"engine must be one of {', '.join(ENGINES)}, not {engine!r}. "
              "Set engine: in the frontmatter or MODEL for the hub.", "engine")
    if spec.input not in INPUT_MODES:
        _fail(source, "input must be forward or never", "input")
    if len(set(spec.tools)) != len(spec.tools):
        _fail(source, "tools lists a name twice", "tools")
    schema = None
    if spec.response_schema:
        path = (folder / spec.response_schema).resolve()
        if folder.resolve() not in path.parents or not path.is_file():
            _fail(source, f"response_schema {spec.response_schema!r} must be a file in the agent's folder",
                  "response_schema")
        raw = path.read_bytes()
        if len(raw) > MAX_SCHEMA_BYTES:
            _fail(source, "response_schema is larger than 64 KiB", "response_schema")
        try:
            schema = json.loads(raw)
            import jsonschema

            jsonschema.Draft202012Validator.check_schema(schema)
        except Exception as exc:  # noqa: BLE001
            _fail(source, f"response_schema is not a valid JSON Schema: {exc}", "response_schema")
        if schema.get("type") != "object":
            _fail(source, "response_schema must describe a JSON object", "response_schema")
    station = Station(name=spec.name, description=spec.description, instructions=loaded.instructions,
                      engine=engine, model=model, tools=tuple(spec.tools), schema=schema, input=spec.input,
                      sandbox=spec.sandbox, timeout_seconds=spec.timeout_seconds, source=source)
    return Station(**{**station.__dict__, "digest": _digest(station)})


def available_tools(hub_dir) -> set[str]:
    """Tool names a station may list: the hub's pre-shipped and local tools,
    Hubzoid's station tools, and the sandbox tools (with a sandbox only)."""
    from ..sandbox import tools as sandbox_tools
    from .tools import hub_registry

    return (set(hub_registry(Path(hub_dir))) | set(STATION_TOOLS) | set(sandbox_tools.NAMES)
            | set(sandbox_tools.HUB_ALIASES))


def validate(hub_dir) -> dict:
    """Check every station without running anything: `{valid, checks}`."""
    hub_dir = Path(hub_dir)
    checks: list[dict] = []
    try:
        stations = load_all(hub_dir)
    except WorkItemError as exc:
        return {"valid": False, "checks": [{"code": exc.code, "path": exc.fields.get("file"),
                                            "status": "fail", "message": exc.message}]}
    except ValueError as exc:
        return {"valid": False, "checks": [{"code": "invalid_station_definition", "path": None,
                                            "status": "fail", "message": str(exc)}]}
    known = available_tools(hub_dir)
    for station in stations.values():
        unknown = [t for t in station.tools if t not in known]
        checks.append({"code": "unknown_tool" if unknown else "ok", "path": station.source,
                       "status": "fail" if unknown else "pass",
                       "message": (f"{station.name}: unknown tools {', '.join(unknown)}. Available: "
                                   f"{', '.join(sorted(known))}." if unknown else
                                   f"{station.name}: {station.engine}"
                                   f"{'/' + station.model if station.model else ''}, "
                                   f"tools [{', '.join(station.tools) or 'none'}], "
                                   f"{'schema' if station.schema else 'no schema'}, {station.digest[:19]}")})
        from ..sandbox import tools as sandbox_tools
        boxed = [t for t in station.tools if t in sandbox_tools.NAMES or t in sandbox_tools.HUB_ALIASES]
        if boxed and not station.sandbox:
            checks.append({"code": "sandbox_tool_without_sandbox", "path": station.source, "status": "fail",
                           "message": f"{station.name}: {', '.join(boxed)} need a sandbox profile. Add "
                                      "sandbox: <profile> to its frontmatter."})
        if station.sandbox:
            problem = _sandbox_problem(hub_dir, station.sandbox)
            if problem:
                checks.append({"code": "sandbox_unavailable", "path": station.source, "status": "fail",
                               "message": f"{station.name}: {problem}"})
    if not stations:
        checks.append({"code": "no_stations", "path": None, "status": "pass",
                       "message": "No isolated agents yet. Add isolated: true to an agent's frontmatter."})
    return {"valid": all(c["status"] == "pass" for c in checks), "checks": checks,
            "stations": {name: s.to_json() for name, s in stations.items()}}


def _sandbox_problem(hub_dir, profile: str) -> str | None:
    """Why a station's sandbox profile cannot be used, or None."""
    try:
        from ..sandbox.config import profile_problem
    except ImportError:
        return "sandboxes are not available in this Hubzoid version"
    return profile_problem(hub_dir, profile)
