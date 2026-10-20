# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""Typed workflow inputs: `@workflow(inputs=Model)`.

A workflow's inputs are a Pydantic model. Chat and MCP tools take its JSON
schema, the CLI takes `--input key=value`, and the Console builds its form from
the same schema. Inputs are validated again on every run, so a schedule saved
before a code change pauses with "inputs changed" instead of running with
values the code no longer accepts.

Inputs are short and never secret: they travel in the run's checkpoint, the
schedule row and the Console. A workflow that takes a large value, such as a
diff, raises its own limit with `max_input_kb` (not for schedulable ones). A field marked hidden
(`Field(json_schema_extra={"hidden": True})`) shows as •••• in listings.
"""
from __future__ import annotations

import json

#: Largest serialized inputs accepted for one run or schedule, unless the
#: workflow sets `max_input_kb` (at most MAX_INPUT_KB).
MAX_BYTES = 4000
MAX_INPUT_KB = 256
HIDDEN = "••••"


class InputError(ValueError):
    """Inputs that don't match a workflow's model. The message is safe to show."""


def is_model(value) -> bool:
    try:
        from pydantic import BaseModel
    except ImportError:  # pragma: no cover - pydantic is a pinned dependency
        return False
    return isinstance(value, type) and issubclass(value, BaseModel)


def required_fields(model) -> list[str]:
    if model is None:
        return []
    return [name for name, f in model.model_fields.items() if f.is_required()]


def schema(model) -> dict:
    """The JSON schema a tool exposes: an object, no definitions to resolve."""
    if model is None:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    raw = model.model_json_schema()
    out = {"type": "object", "properties": raw.get("properties", {}),
           "additionalProperties": False}
    if raw.get("required"):
        out["required"] = list(raw["required"])
    if raw.get("$defs"):
        out["$defs"] = raw["$defs"]
    for prop in out["properties"].values():
        prop.pop("title", None)
    return out


def _first_error(exc) -> str:
    err = exc.errors()[0]
    field = ".".join(str(p) for p in err.get("loc", ())) or "input"
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "literal_error" and ctx.get("expected"):
        rule = "one of " + ctx["expected"].replace("'", "")
    elif kind == "missing":
        rule = "given"
    elif kind == "extra_forbidden":
        return f"{field} isn't an input of this report. Nothing ran."
    elif kind == "value_error" and not err.get("loc"):
        # A model-level validator's message is a whole sentence: show it as written.
        rule = (err.get("msg") or "").removeprefix("Value error, ").strip().rstrip(".")
        return f"{rule[:1].upper() + rule[1:]}. Nothing ran." if rule else "Those inputs aren't valid. Nothing ran."
    else:
        rule = (err.get("msg") or "valid").removeprefix("Value error, ").rstrip(".")
        rule = rule[:1].lower() + rule[1:]
        if rule.startswith("input should be "):
            rule = rule.removeprefix("input should be ")
    return f"{field} must be {rule}. Nothing ran."


def validate(model, data, *, max_bytes: int = MAX_BYTES) -> object | None:
    """A model instance from `data` (a dict, or None for defaults). Raises
    InputError with one short, safe sentence."""
    if model is None:
        if data:
            raise InputError("This report takes no inputs. Nothing ran.")
        return None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise InputError("Inputs must be named values. Nothing ran.")
    if len(json.dumps(data, default=str)) > max_bytes:
        raise InputError("Those inputs are too long. Nothing ran.")
    from pydantic import ValidationError

    strict = model.model_config.get("extra") is None
    try:
        if strict:
            unknown = set(data) - set(model.model_fields)
            if unknown:
                raise InputError(f"{sorted(unknown)[0]} isn't an input of this report. Nothing ran.")
        return model.model_validate(data)
    except ValidationError as exc:
        raise InputError(_first_error(exc)) from None


def dump(instance) -> dict | None:
    """Plain JSON-safe data for a checkpoint or a schedule row."""
    if instance is None:
        return None
    return instance.model_dump(mode="json")


def hidden_fields(model) -> set[str]:
    if model is None:
        return set()
    out = set()
    for name, f in model.model_fields.items():
        extra = f.json_schema_extra if isinstance(f.json_schema_extra, dict) else {}
        if extra.get("hidden"):
            out.add(name)
    return out


def display(model, data: dict | None, *, limit: int = 120) -> str:
    """A short, safe summary for listings: values only, hidden ones masked,
    empty ones left out. "Intro Spandana, month"."""
    if not data:
        return ""
    hidden = hidden_fields(model)
    parts = []
    for name, value in data.items():
        if value in (None, "", [], {}):
            continue
        parts.append(HIDDEN if name in hidden else str(value))
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_cli(pairs) -> dict:
    """`--input key=value` pairs to a dict. Values that look like JSON (numbers,
    true/false, lists) are parsed; everything else is a string."""
    out: dict = {}
    for pair in pairs or ():
        if "=" not in pair:
            raise InputError(f"Write each input as key=value, got {pair!r}.")
        key, value = pair.split("=", 1)
        key = key.strip()
        try:
            out[key] = json.loads(value)
            if isinstance(out[key], str):
                out[key] = value
        except ValueError:
            out[key] = value
    return out


def display_with_schema(schema: dict | None, data: dict | None, *, limit: int = 120) -> str:
    """`display` for a process that has the published JSON schema, not the
    model: a property marked "hidden" is masked. With no schema (the workflow
    was removed or renamed), nothing can be shown safely, so every value is."""
    if not data:
        return ""
    props = (schema or {}).get("properties") or {}
    parts = []
    for name, value in data.items():
        if value in (None, "", [], {}):
            continue
        parts.append(HIDDEN if schema is None or (props.get(name) or {}).get("hidden") else str(value))
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"
