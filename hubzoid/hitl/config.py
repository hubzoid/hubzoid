"""``<hub>/hitl.yaml``: when the agent asks a person first.

The file is optional. Without it no tool asks, exactly as before (eve's
default, ``never``), and the agent may ask questions. A hub file, versioned
with the hub, edited by the builder or ``hubzoid hitl apply``:

.. code-block:: yaml

    version: 1
    questions: true            # the agent may ask the person a question
    approvers: requester       # or a list of account ids or emails
    tools:
      reserve_stock: {ask: always}
      send_report: {ask: once, allow_always: true}
    connectors:
      github: {ask: writes}    # tools the server does not mark read-only
      crm: {ask: always}
    agents:
      intake: {questions: false}

``ask`` is ``never`` (run without asking), ``once`` (ask the first time in a
chat), ``always`` (ask every time) or ``auto`` (a quick model check runs
routine calls and asks a person for anything risky or unclear). Connectors
also take ``writes``. ``allow_always`` lets a person pick "Always allow for
me" on that tool's card.

A tool's own setting wins over its connector's. Consequential writes that a
hub sends through write modes (email, ERP) keep their own person-required
approval: nothing in this file can turn that off.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..control import yamlfile

FILE = "hitl.yaml"
ASK = ("never", "once", "always", "auto")
CONNECTOR_ASK = ASK + ("writes",)


class ConfigError(ValueError):
    def __init__(self, message: str, *, path: str | None = None, line: int | None = None):
        super().__init__(message)
        self.message = message
        self.path = path
        self.line = line

    def as_dict(self) -> dict:
        return {"message": self.message, "path": self.path, "line": self.line}


@dataclass(frozen=True)
class ToolRule:
    ask: str = "never"
    allow_always: bool = False


@dataclass(frozen=True)
class Config:
    questions: bool = True
    approvers: tuple[str, ...] = ("requester",)
    tools: dict = field(default_factory=dict)        # name -> ToolRule
    connectors: dict = field(default_factory=dict)   # name -> ToolRule
    agents: dict = field(default_factory=dict)       # name -> {"questions": bool}
    digest: str = ""                                 # sha256 of the file bytes, "" when absent
    invalid: bool = False                            # the file exists but does not validate

    def rule_for(self, tool: str, connector: str | None = None) -> ToolRule:
        """The rule for a tool: its own entry, then its connector's, then never.
        A file that does not validate asks for every tool until it is fixed."""
        if self.invalid:
            return ToolRule(ask="always")
        if tool in self.tools:
            return self.tools[tool]
        if connector and connector in self.connectors:
            return self.connectors[connector]
        return ToolRule()

    def questions_for(self, agent: str | None) -> bool:
        if agent and agent in self.agents and "questions" in self.agents[agent]:
            return bool(self.agents[agent]["questions"])
        return self.questions


def _fail(loaded, pointer: str, message: str):
    raise ConfigError(message, path=pointer or "/", line=loaded.line_of(pointer) if loaded else None)


def _rule(loaded, pointer: str, value, allowed: tuple[str, ...]) -> ToolRule:
    if isinstance(value, str):
        value = {"ask": value}
    if not isinstance(value, dict):
        _fail(loaded, pointer, "Each entry is a mapping such as {ask: always}.")
    unknown = sorted(set(value) - {"ask", "allow_always"})
    if unknown:
        _fail(loaded, f"{pointer}/{unknown[0]}", f"Unknown key {unknown[0]!r}. Use ask and allow_always.")
    ask = value.get("ask", "never")
    if ask not in allowed:
        _fail(loaded, f"{pointer}/ask", f"ask must be one of: {', '.join(allowed)}.")
    allow_always = value.get("allow_always", False)
    if not isinstance(allow_always, bool):
        _fail(loaded, f"{pointer}/allow_always", "allow_always is true or false.")
    return ToolRule(ask=ask, allow_always=allow_always)


def parse(text: str, *, path: str | None = None) -> Config:
    """Validate the whole document. Raises ConfigError naming the key and line."""
    try:
        loaded = yamlfile.loads(text, path=path)
    except yamlfile.YamlError as exc:
        raise ConfigError(exc.message, path=exc.path, line=exc.line) from exc
    data = loaded.data
    unknown = sorted(set(data) - {"version", "questions", "approvers", "tools", "connectors", "agents"})
    if unknown:
        _fail(loaded, f"/{unknown[0]}", f"Unknown key {unknown[0]!r}.")
    if data.get("version") != 1:
        _fail(loaded, "/version", "version must be 1.")
    questions = data.get("questions", True)
    if not isinstance(questions, bool):
        _fail(loaded, "/questions", "questions is true or false.")
    approvers = data.get("approvers", "requester")
    if approvers == "requester":
        approvers_t: tuple[str, ...] = ("requester",)
    elif isinstance(approvers, list) and approvers and all(isinstance(a, str) and a.strip() for a in approvers):
        approvers_t = tuple(a.strip() for a in approvers)
    elif isinstance(approvers, dict) and set(approvers) == {"group"}:
        _fail(loaded, "/approvers", "Approver groups are not available yet. Name people instead.")
    else:
        _fail(loaded, "/approvers", "approvers is requester, or a list of account ids or emails.")
    tools: dict = {}
    for name, value in (data.get("tools") or {}).items():
        tools[str(name)] = _rule(loaded, f"/tools/{name}", value, ASK)
    connectors: dict = {}
    for name, value in (data.get("connectors") or {}).items():
        connectors[str(name)] = _rule(loaded, f"/connectors/{name}", value, CONNECTOR_ASK)
    agents: dict = {}
    for name, value in (data.get("agents") or {}).items():
        if not isinstance(value, dict) or set(value) - {"questions"} or \
                not isinstance(value.get("questions", True), bool):
            _fail(loaded, f"/agents/{name}", "An agent entry takes questions: true or false.")
        agents[str(name)] = dict(value)
    return Config(questions=questions, approvers=approvers_t, tools=tools, connectors=connectors,
                  agents=agents, digest=hashlib.sha256(text.encode()).hexdigest())


_cache: dict[str, tuple[float, Config]] = {}


def load(hub_dir: Path) -> Config:
    """The hub's current settings (no file: the defaults). Read from disk when
    it changed, so an edit applies to the next tool call without a restart. A
    file that does not validate keeps the safe default for asking (``always``
    for every tool) rather than silently running everything."""
    path = Path(hub_dir) / FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return Config()
    cached = _cache.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        cfg = parse(path.read_text(encoding="utf-8"), path=str(path))
    except (OSError, ConfigError):
        cfg = Config(digest="invalid", invalid=True)
    _cache[str(path)] = (mtime, cfg)
    return cfg


def broken(hub_dir: Path) -> ConfigError | None:
    """The validation error of the hub's file, or None (also when absent)."""
    path = Path(hub_dir) / FILE
    if not path.is_file():
        return None
    try:
        parse(path.read_text(encoding="utf-8"), path=str(path))
    except ConfigError as exc:
        return exc
    return None


def current_text(hub_dir: Path) -> str | None:
    path = Path(hub_dir) / FILE
    return path.read_text(encoding="utf-8") if path.is_file() else None


def revision_of(text: str | None) -> str:
    """The file's revision for a compare-and-set write: its sha256, or 'absent'."""
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else "absent"


def apply(hub_dir: Path, text: str, *, expected_revision: str) -> Config:
    """Validate ``text`` and write it atomically, only if the file is still at
    ``expected_revision`` ('absent' to create it)."""
    cfg = parse(text)
    current = current_text(hub_dir)
    if revision_of(current) != expected_revision:
        raise ConfigError(f"The file changed since you read it (now {revision_of(current)[:12]}). "
                          "Export it again, merge and apply.", path="revision")
    path = Path(hub_dir) / FILE
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".hitl.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _cache.pop(str(path), None)
    return cfg
