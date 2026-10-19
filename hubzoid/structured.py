"""Structured (JSON) answers from a model: the instruction and a tolerant parser.

Runtime-neutral: both backends send the same instruction and parse the reply the
same way, so `hub.call_llm(..., response_format="json")` behaves identically on
LiteLLM models and on `claude-local`.
"""
from __future__ import annotations

import json
import re


class ModelOutputError(ValueError):
    """The model answered, but not with the JSON that was asked for."""

    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw

    def __reduce__(self):
        # Workflow errors are pickled into the run's history.
        return (self.__class__, (str(self), self.raw))


def json_instruction(schema: dict | None) -> str:
    """The instruction appended to a prompt when JSON is required."""
    if schema:
        return (
            "\n\nRespond with only a JSON object that matches this JSON Schema. "
            "No prose, no code fences.\n" + json.dumps(schema, separators=(",", ":"))
        )
    return "\n\nRespond with only a JSON object. No prose, no code fences."


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str):
    """Parse the JSON value in a model reply: the whole reply, a fenced block,
    or the outermost {...} / [...] span. Raises ModelOutputError otherwise."""
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]
    for open_, close in (("{", "}"), ("[", "]")):
        start, end = text.find(open_), text.rfind(close)
        if 0 <= start < end:
            candidates.append(text[start:end + 1])
    for chunk in candidates:
        if not chunk:
            continue
        try:
            return json.loads(chunk)
        except ValueError:
            continue
    raise ModelOutputError("the model did not return valid JSON", text)


_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
          "boolean": bool, "null": type(None)}


def schema_errors(value, schema: dict, path: str = "") -> list[str]:
    """Where ``value`` breaks the JSON Schema subset helpers use (type,
    properties, required, additionalProperties false, items, enum,
    min/maxLength, min/maxItems, minimum/maximum). Empty when it fits."""
    if not isinstance(schema, dict):
        return []
    where = path or "/"
    want = schema.get("type")
    if want:
        types = want if isinstance(want, list) else [want]
        py = tuple(t for name in types for t in ((_TYPES[name],) if not isinstance(_TYPES.get(name), tuple)
                                                 else _TYPES[name]) if name in _TYPES)
        if py and (not isinstance(value, py) or (isinstance(value, bool) and bool not in py)):
            return [f"{where}: expected {', '.join(types)}"]
    errs: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: not one of the allowed values")
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", float("inf")):
            errs.append(f"{where}: longer than {schema['maxLength']} characters")
        if len(value) < schema.get("minLength", 0):
            errs.append(f"{where}: shorter than {schema['minLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{where}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{where}: above {schema['maximum']}")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", float("inf")):
            errs.append(f"{where}: more than {schema['maxItems']} items")
        if len(value) < schema.get("minItems", 0):
            errs.append(f"{where}: fewer than {schema['minItems']} items")
        for i, item in enumerate(value):
            errs += schema_errors(item, schema.get("items") or {}, f"{path}/{i}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errs.append(f"{path}/{key}: missing")
        if schema.get("additionalProperties") is False:
            errs += [f"{path}/{k}: not allowed" for k in value if k not in props]
        for key, sub in props.items():
            if key in value:
                errs += schema_errors(value[key], sub, f"{path}/{key}")
    return errs
