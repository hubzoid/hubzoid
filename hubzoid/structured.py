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
