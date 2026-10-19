"""Settings a hub declares, and their values.

    <hub>/settings.yaml        declarations: name, type, required, default, example,
                               secret, regex, enum, minimum, maximum, env
    <hub>/hub.settings.yaml    values: `values.KEY` for settings, `secrets.KEY_ref`
                               for secret references (secret:... or env:...)

Both files are optional, so a hub without them behaves as before. The files are
the authority: the API and CLI validate and write them, and record a revision
so two editors cannot overwrite each other (`revision`).

A non-secret value comes from, in order: the environment variable named by its
`env` key (if any), `hub.settings.yaml`, then its default. A secret setting has
no value here, only a reference: `secrets.KEY_ref` when given, else
`env:KEY`, so a key in `.env` or stored with `hubzoid secrets set` satisfies it.
Secret values are only ever read through `hubzoid.secretstore.resolve`.

AGENTS.md and SKILL.md may use `{{settings.KEY}}`. The loaders replace each
token with the value, once, with no evaluation and no second pass. An unknown,
secret or unset key stops the hub from loading, naming the file and line, so
every runtime gets the same text or none starts.

Regex constraints use Pydantic's Rust engine, which runs in linear time.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError, field_validator

from .yamlfile import HubFileError, load

DECLARATIONS = "settings.yaml"
VALUES = "hub.settings.yaml"
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*settings\.([A-Za-z0-9_]+)\s*\}\}")
MAX_STRING = 16 * 1024
MAX_LIST = 128
MAX_ITEM = 1024
MAX_SETTINGS = 256
TYPES = ("string", "integer", "number", "boolean", "string_list")


class Declaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    required: bool = False
    type: Literal["string", "integer", "number", "boolean", "string_list"] = "string"
    default: Any = None
    example: Any = None
    secret: bool = False
    regex: str | None = Field(default=None, max_length=256)
    enum: list[str | int | float] | None = None
    minimum: float | None = None
    maximum: float | None = None
    env: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError("a setting name is UPPER_CASE letters, digits and underscores, up to 64")
        return value

    @field_validator("env")
    @classmethod
    def _env(cls, value: str | None) -> str | None:
        if value is not None and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
            raise ValueError("env names an environment variable")
        return value


class DeclarationsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    settings: list[Declaration] = Field(default_factory=list, max_length=MAX_SETTINGS)


class ValuesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    values: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


@dataclass
class Problem:
    """One validation error, safe to show: never a secret value."""
    key: str | None
    code: str
    message: str
    file: str | None = None
    line: int | None = None

    def public(self) -> dict:
        return {k: v for k, v in {"key": self.key, "code": self.code, "message": self.message,
                                  "file": self.file, "line": self.line}.items() if v is not None}


@dataclass
class Resolved:
    """One declared setting with where its value comes from. ``value`` is None
    for a secret (only ``ref`` and ``available`` are known). ``ref`` is None
    when the reference given is not a valid one: it may be a pasted credential,
    so it is never shown."""
    decl: Declaration
    value: Any = None
    source: str | None = None          # environment | hub.settings.yaml | default | None
    ref: str | None = None             # secrets: the reference in use, when valid
    available: bool | None = None      # secrets: whether the reference resolves now
    stored: Any = None                 # non-secret: the raw value in hub.settings.yaml, if any
    problems: list[Problem] = field(default_factory=list)

    def public(self) -> dict:
        d = self.decl
        out = {"name": d.name, "type": d.type, "required": d.required, "secret": d.secret,
               "description": d.description, "example": d.example if not d.secret else None,
               "enum": d.enum, "source": self.source,
               "status": "invalid" if self.problems else ("set" if self.is_set else "missing"),
               "problems": [p.public() for p in self.problems]}
        if d.secret:
            out.update(ref=self.ref, available=bool(self.available))
        else:
            out.update(value=self.value, default=d.default, env=d.env, stored=self.stored)
        return out

    @property
    def is_set(self) -> bool:
        return bool(self.available) if self.decl.secret else self.value is not None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _model_error(path: Path, exc: ValidationError) -> HubFileError:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"])
    return HubFileError(f"{path}: {where}: {first['msg']}")


def declarations(hub_dir: Path) -> list[Declaration]:
    """The hub's declarations ([] without settings.yaml). Raises HubFileError."""
    path = Path(hub_dir) / DECLARATIONS
    if not path.is_file():
        return []
    try:
        parsed = DeclarationsFile.model_validate(load(path))
    except ValidationError as exc:
        raise _model_error(path, exc) from None
    seen: set[str] = set()
    for decl in parsed.settings:
        if decl.name in seen:
            raise HubFileError(f"{path}: setting {decl.name} is declared twice")
        seen.add(decl.name)
        problem = _declaration_problem(decl)
        if problem:
            raise HubFileError(f"{path}: {decl.name}: {problem}")
    return parsed.settings


def _declaration_problem(decl: Declaration) -> str | None:
    if decl.secret and decl.default is not None:
        return "a secret setting cannot have a default"
    if decl.secret and decl.example is not None and not str(decl.example).startswith(("secret:", "env:")):
        return "a secret setting's example is a reference (secret:... or env:...), never a value"
    if decl.regex is not None:
        if decl.type not in ("string", "string_list"):
            return "regex applies to string settings only"
        try:
            TypeAdapter(Annotated[str, StringConstraints(pattern=decl.regex)])
        except Exception:  # noqa: BLE001 - pydantic-core raises SchemaError for unsupported patterns
            return "regex is not valid (lookaround and backreferences are not supported)"
    if decl.default is not None:
        _, why = coerce(decl, decl.default)
        if why:
            return f"default: {why}"
    return None


def values_file(hub_dir: Path) -> ValuesFile | None:
    """``hub.settings.yaml`` parsed, or None when absent. Raises HubFileError."""
    path = Path(hub_dir) / VALUES
    if not path.is_file():
        return None
    return parse_values(load(path), path)


def parse_values(data: Mapping, path: Path | str = VALUES) -> ValuesFile:
    try:
        return ValuesFile.model_validate(dict(data))
    except ValidationError as exc:
        raise _model_error(Path(path), exc) from None


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def coerce(decl: Declaration, raw: Any) -> tuple[Any, str | None]:
    """(value, None) or (None, why) for a value of this declaration. Strings
    from the environment are parsed (true/false, numbers, comma lists)."""
    t = decl.type
    if t == "string":
        if not isinstance(raw, str):
            return None, "must be text"
        value = raw
        if len(value.encode()) > MAX_STRING:
            return None, "is longer than 16 KiB"
    elif t == "integer":
        if isinstance(raw, str):
            try:
                raw = int(raw.strip())
            except ValueError:
                return None, "must be a whole number"
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, "must be a whole number"
        value = raw
    elif t == "number":
        if isinstance(raw, str):
            try:
                raw = float(raw.strip())
            except ValueError:
                return None, "must be a number"
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return None, "must be a finite number"
        value = raw
    elif t == "boolean":
        if isinstance(raw, str) and raw.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            raw = raw.strip().lower() in ("true", "1", "yes", "on")
        if not isinstance(raw, bool):
            return None, "must be true or false"
        value = raw
    else:  # string_list
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",") if part.strip()]
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            return None, "must be a list of text items"
        if len(raw) > MAX_LIST or any(len(x.encode()) > MAX_ITEM for x in raw):
            return None, "has more than 128 items or an item over 1 KiB"
        value = raw
    if decl.enum is not None:
        items = value if isinstance(value, list) else [value]
        if any(item not in decl.enum for item in items):
            return None, "must be one of " + ", ".join(map(str, decl.enum))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if decl.minimum is not None and value < decl.minimum:
            return None, f"must be at least {decl.minimum:g}"
        if decl.maximum is not None and value > decl.maximum:
            return None, f"must be at most {decl.maximum:g}"
    if decl.regex is not None:
        check = TypeAdapter(Annotated[str, StringConstraints(pattern=decl.regex)])
        for item in value if isinstance(value, list) else [value]:
            try:
                check.validate_python(item)
            except ValidationError:
                return None, "does not match the expected pattern" + (
                    f" (example: {decl.example})" if decl.example is not None else "")
    return value, None


def _secret_available(hub_dir: Path, ref: str, env: Mapping[str, str]) -> tuple[bool, Problem | None]:
    from ..secretstore import RefError, parse

    try:
        parsed = parse(ref)
    except RefError:
        # The text may be a pasted credential: never repeat it.
        return False, Problem(None, "invalid_ref", "is not a valid reference. Write secret:hub/<hub>/<name>, "
                                                   "secret:deployment/<name> or env:<NAME>.")
    if parsed.provider == "env":
        return bool((env.get(parsed.name) or "").strip()), None
    if parsed.scope in ("hub", "channel"):
        from ..assertions import hub_key

        if parsed.hub != hub_key(Path(hub_dir)):
            return False, Problem(None, "permission_denied", "A hub can use only its own secrets.")
    from ..secretstore import store

    meta = store.get_meta(hub_dir, scope=parsed.scope, target=parsed.target, name=parsed.name)
    return meta is not None, None


def _secret_env(hub_dir: Path, env: Mapping[str, str] | None) -> dict[str, str]:
    """What an env: reference can see: the given environment, else this
    process's environment plus the hub's layers and stored secrets."""
    if env is not None:
        return dict(env)
    out = dict(os.environ)
    try:
        from .. import config_secrets as cs
        from ..assertions import hub_key
        from ..secretstore import store

        for _layer, _source, values in cs._layers(Path(hub_dir), cs._start_env(), fetch_secrets=False,
                                                  strict=False):
            out.update({k: v for k, v in values.items() if v.strip()})
        for scope, target in (("deployment", ""), ("hub", hub_key(Path(hub_dir)))):
            for meta in store.list_meta(hub_dir, scope=scope, target=target):
                out.setdefault(meta.name, "<stored>")
    except Exception:  # noqa: BLE001 - availability is best effort here; doctor reports layers
        pass
    return out


def resolve(hub_dir: Path, *, values: ValuesFile | None = None, env: Mapping[str, str] | None = None,
            decls: list[Declaration] | None = None, with_secrets: bool = True) -> list[Resolved]:
    """Every declared setting with its effective value or reference. ``values``
    replaces hub.settings.yaml (to validate a candidate). Without
    ``with_secrets`` secret settings are listed but not looked up. Raises
    HubFileError when a file cannot be parsed."""
    hub_dir = Path(hub_dir)
    decls = declarations(hub_dir) if decls is None else decls
    values = values_file(hub_dir) if values is None else values
    vals = values.values if values else {}
    refs = values.secrets if values else {}
    environment = os.environ if env is None else env
    secret_env = _secret_env(hub_dir, env) if with_secrets and any(d.secret for d in decls) else {}
    out: list[Resolved] = []
    for decl in decls:
        r = Resolved(decl)
        if decl.secret and not with_secrets:
            pass  # listed only: render and workflows never read a secret's reference here
        elif decl.secret:
            ref = refs.get(f"{decl.name}_ref") or f"env:{decl.name}"
            r.source = "hub.settings.yaml" if f"{decl.name}_ref" in refs else "default"
            r.available, problem = _secret_available(hub_dir, ref, secret_env)
            if problem:
                problem.key = decl.name
                if problem.code == "invalid_ref":
                    problem.message = f"{decl.name}_ref {problem.message}"
                r.problems.append(problem)
            else:
                r.ref = ref
                if decl.required and not r.available:
                    r.problems.append(Problem(decl.name, "secret_unavailable",
                                              f"{decl.name} is required. Set it with hubzoid secrets set "
                                              f"--name {decl.name}, or point {decl.name}_ref at a secret."))
        else:
            raw, source = None, None
            r.stored = vals.get(decl.name)
            if decl.env and (environment.get(decl.env) or "").strip():
                raw, source = environment[decl.env], "environment"
            elif decl.name in vals:
                raw, source = vals[decl.name], VALUES
            elif decl.default is not None:
                raw, source = decl.default, "default"
            if raw is not None:
                value, why = coerce(decl, raw)
                if why:
                    r.problems.append(Problem(decl.name, "setting_invalid", f"{decl.name} {why}."))
                else:
                    r.value, r.source = value, source
            elif decl.required:
                example = f" For example: {decl.example}." if decl.example is not None else ""
                r.problems.append(Problem(decl.name, "setting_required",
                                          f"{decl.name} is required: {decl.description or 'set a value'}.{example}"))
        out.append(r)
    return out


def check_values_file(hub_dir: Path, values: ValuesFile, decls: list[Declaration]) -> list[Problem]:
    """Keys in a values file that no declaration allows."""
    by_name = {d.name: d for d in decls}
    problems = []
    for key in values.values:
        decl = by_name.get(key)
        if decl is None:
            problems.append(Problem(key, "setting_unknown", f"{key} is not declared in settings.yaml."))
        elif decl.secret:
            problems.append(Problem(key, "secret_literal_forbidden",
                                    f"{key} is a secret. Put a reference in secrets.{key}_ref, never the value."))
    for key, ref in values.secrets.items():
        name = key[:-4] if key.endswith("_ref") else None
        decl = by_name.get(name or "")
        if name is None or decl is None or not decl.secret:
            problems.append(Problem(key, "setting_unknown",
                                    f"{key} does not name a declared secret setting (write <NAME>_ref)."))
        elif not isinstance(ref, str) or not ref.startswith(("secret:", "env:")):
            problems.append(Problem(key, "secret_literal_forbidden",
                                    f"{key} holds a reference such as secret:hub/<hub>/<name> or env:<NAME>, "
                                    "never the value."))
    return problems


def validate(hub_dir: Path, values: ValuesFile | None = None, *, env: Mapping[str, str] | None = None
             ) -> tuple[list[Resolved], list[Problem]]:
    """Every problem at once: unknown keys, literals in secrets, missing and
    invalid values. Raises HubFileError for an unparsable declaration file."""
    decls = declarations(hub_dir)
    values = values_file(hub_dir) if values is None else values
    problems = check_values_file(hub_dir, values, decls) if values else []
    resolved = resolve(hub_dir, values=values, env=env, decls=decls)
    for r in resolved:
        problems.extend(r.problems)
    return resolved, problems


# What a running process uses: read once, kept until the process restarts, so
# a saved change never alters work in progress ("applies after a restart").
_snapshots: dict[str, tuple[dict[str, Resolved], dict[str, str]]] = {}
_snapshot_lock = threading.Lock()


def _snapshot(hub_dir: Path) -> tuple[dict[str, Resolved], dict[str, str]]:
    """({NAME: Resolved} without secret lookups, {NAME: secret reference}).
    Raises HubFileError the first time if a file cannot be read."""
    key = str(Path(hub_dir).resolve())
    with _snapshot_lock:
        snap = _snapshots.get(key)
        if snap is None:
            decls = declarations(hub_dir)
            values = values_file(hub_dir)
            resolved = {r.decl.name: r for r in resolve(hub_dir, values=values, decls=decls, with_secrets=False)}
            refs = {d.name: (values.secrets.get(f"{d.name}_ref") if values else None) or f"env:{d.name}"
                    for d in decls if d.secret}
            snap = _snapshots[key] = (resolved, refs)
        return snap


def forget() -> None:
    """Drop the snapshots (tests, or a process that reloads on purpose)."""
    with _snapshot_lock:
        _snapshots.clear()


def effective(hub_dir: Path) -> dict[str, Any]:
    """{NAME: value} for every non-secret setting that has a value, as this
    process read them at first use."""
    try:
        resolved, _ = _snapshot(hub_dir)
    except HubFileError:
        return {}
    return {n: r.value for n, r in resolved.items() if not r.decl.secret and r.value is not None}


def secret_ref(hub_dir: Path, name: str) -> str | None:
    """The reference a declared secret setting uses (as read at first use),
    or None if not declared."""
    try:
        _, refs = _snapshot(hub_dir)
    except HubFileError:
        return None
    return refs.get(name)


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------
def render(hub_dir: Path, text: str, source: Path) -> str:
    """Replace each ``{{settings.KEY}}`` once. Raises HubFileError naming the
    file and line for an unknown, secret or unset key."""
    if "{{" not in text or not PLACEHOLDER_RE.search(text):
        return text
    values, _ = _snapshot(hub_dir)
    decls = {name: r.decl for name, r in values.items()}

    try:
        whole = Path(source).read_text()  # the file, frontmatter included, for line numbers
    except OSError:
        whole = text

    def line(token: str) -> int:
        pos = whole.find(token)
        return whole.count("\n", 0, pos) + 1 if pos >= 0 else 0

    for match in PLACEHOLDER_RE.finditer(text):
        key = match.group(1)
        where = f"{source}: line {line(match.group(0))}"
        decl = decls.get(key)
        if decl is None:
            raise HubFileError(f"{where}: {{{{settings.{key}}}}} names no setting in settings.yaml")
        if decl.secret:
            raise HubFileError(f"{where}: {{{{settings.{key}}}}} is a secret and cannot go into instructions")
        if values[key].problems or values[key].value is None:
            raise HubFileError(f"{where}: {{{{settings.{key}}}}} has no valid value. Run hubzoid doctor.")

    def sub(match: re.Match) -> str:
        value = values[match.group(1)].value
        return value if isinstance(value, str) else json.dumps(value)

    return PLACEHOLDER_RE.sub(sub, text)


# ---------------------------------------------------------------------------
# Writing, with a revision
# ---------------------------------------------------------------------------
def digest(hub_dir: Path) -> str:
    path = Path(hub_dir) / VALUES
    raw = path.read_bytes() if path.is_file() else b""
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _meta_keys(hub_dir: Path) -> tuple[str, str]:
    from ..assertions import hub_key

    key = hub_key(Path(hub_dir))
    return f"hub_settings:{key}:revision", f"hub_settings:{key}:digest"


@contextmanager
def _locked(hub_dir: Path):
    """One writer at a time for this hub's values file, across processes."""
    from .._file_lock import lock, unlock

    path = Path(hub_dir) / ".hubzoid" / "hub.settings.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        lock(fd)
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


def revision(hub_dir: Path, conn=None) -> int:
    """The values file's revision: it moves on with every change, including a
    hand edit, which is noticed the next time anyone looks. 0 when absent."""
    from sqlalchemy import text

    from ..secretstore.store import engine

    if not (Path(hub_dir) / VALUES).is_file():
        return 0
    rev_key, dig_key = _meta_keys(hub_dir)
    current = digest(hub_dir)

    def run(c) -> int:
        rows = dict(c.execute(text("SELECT k, v FROM hz_meta WHERE k IN (:r, :d)"),
                              {"r": rev_key, "d": dig_key}).all())
        rev = int(rows.get(rev_key) or 0)
        if rows.get(dig_key) != current:
            rev += 1
            for k, v in ((rev_key, str(rev)), (dig_key, current)):
                if k in rows:
                    c.execute(text("UPDATE hz_meta SET v = :v WHERE k = :k"), {"k": k, "v": v})
                else:
                    c.execute(text("INSERT INTO hz_meta (k, v) VALUES (:k, :v)"), {"k": k, "v": v})
        return rev

    if conn is not None:
        return run(conn)
    with _locked(hub_dir), engine(hub_dir).begin() as c:
        return run(c)


class Conflict(Exception):
    def __init__(self, current: int):
        super().__init__(f"current revision {current}")
        self.current = current


def write(hub_dir: Path, document: Mapping, *, expected_revision: int | str | None) -> int:
    """Write hub.settings.yaml from a validated document and return its new
    revision. ``expected_revision`` is an integer, ``"absent"`` (create only)
    or None. Raises Conflict when it does not match."""
    import yaml
    from sqlalchemy import text

    from ..secretstore.store import engine

    from .yamlfile import MAX_BYTES

    path = Path(hub_dir) / VALUES
    body = yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True)
    header = "# Hub setting values. Secrets are references only (secret:... or env:...).\n"
    raw = (header + body).encode()
    if len(raw) > MAX_BYTES:
        raise HubFileError(f"{VALUES} would be larger than 1 MiB. Shorten the longest values.")
    with _locked(hub_dir), engine(hub_dir).begin() as conn:
        current = revision(hub_dir, conn)
        if expected_revision == "absent" and path.is_file():
            raise Conflict(current)
        if isinstance(expected_revision, int) and expected_revision != current:
            raise Conflict(current)
        fd, tmp = tempfile.mkstemp(prefix=".hub.settings.", dir=str(Path(hub_dir)))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return revision(hub_dir, conn)
