"""Tool call details for eval results: safe arguments, previews, one-line views.

A case records every tool call it made: the tool's name, its arguments, whether
it succeeded, the error when it did not, how long it took, which turn made it
and, when the runtime reports it, the start of what it returned. Results files
are kept on disk and shown in the CLI and the Console, so what is stored is
bounded and scrubbed:

  * arguments are a JSON object of at most about 2 KB (long strings shortened,
    then the whole object replaced by a preview if it is still too large);
  * values under secret-looking keys (token, password, secret, api_key,
    authorization) are replaced by "[redacted]", in arguments and in result
    previews;
  * a result preview is at most 500 characters, an error at most 300.

Pure functions, no model and no runtime: testable on their own.
"""
from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[redacted]"

ARGS_MAX_JSON = 2048          # about 2 KB of JSON per call
_ARG_STRING_MAX = 500
_ARG_STRING_TIGHT = 100
PREVIEW_MAX = 500
ERROR_MAX = 300

_SECRET_WORDS = ("token", "password", "passwd", "secret", "api_key", "apikey",
                 "authorization")

# `key: value`, `key=value` and `"key": "value"` in free text (a result preview).
_SECRET_TEXT_RE = re.compile(
    r"""(?ix)
    (["']?[\w-]*(?:token|password|passwd|secret|api[_-]?key|authorization)[\w-]*["']?\s*[:=]\s*)
    ("[^"]*"|'[^']*'|[^\s,;}&]+)
    """)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")


def is_secret_key(key: object) -> bool:
    """True for a key whose value should never be stored (`max_tokens`, a
    count, is not a secret)."""
    k = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    if k.endswith("tokens"):
        return False
    return any(word in k for word in _SECRET_WORDS)


def normalize_args(args: Any) -> Any:
    """The runtime's arguments as a value: a JSON string is parsed, anything
    else is returned unchanged. Used for matching, before any shortening."""
    if isinstance(args, str):
        text = args.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return args
    return args


def redact(value: Any) -> Any:
    """`value` with the values of secret-looking keys replaced, recursively."""
    if isinstance(value, dict):
        return {str(k): (REDACTED if is_secret_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = _SECRET_TEXT_RE.sub(lambda m: m.group(1) + REDACTED, value)
        return _BEARER_RE.sub(lambda m: m.group(1) + " " + REDACTED, value)
    return value


def _shorten(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {k: _shorten(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shorten(v, limit) for v in value]
    return value


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def safe_args(args: Any) -> dict | None:
    """Arguments as stored in a results file: a small, redacted JSON object.
    None when the runtime reported no arguments."""
    if args is None:
        return None
    value = normalize_args(args)
    if not isinstance(value, dict):
        value = {"input": value}
    value = _jsonable(redact(value))
    for limit in (_ARG_STRING_MAX, _ARG_STRING_TIGHT):
        shortened = _shorten(value, limit)
        encoded = json.dumps(shortened, ensure_ascii=False)
        if len(encoded) <= ARGS_MAX_JSON:
            return shortened
    return {"preview": encoded[:ARGS_MAX_JSON - 48] + "…"}


def scrub_text(text: str) -> str:
    """Free text with secret-looking `key: value` pairs and bearer credentials
    redacted. A JSON payload is parsed and redacted by key instead."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.dumps(redact(json.loads(stripped)), ensure_ascii=False)
        except ValueError:
            pass
    text = _SECRET_TEXT_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return _BEARER_RE.sub(lambda m: m.group(1) + " " + REDACTED, text)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def preview(text: str | None) -> str | None:
    """A stored result preview: scrubbed, at most PREVIEW_MAX characters."""
    if text is None:
        return None
    text = scrub_text(text).strip()
    return _clip(text, PREVIEW_MAX) if text else None


def error_text(text: str | None) -> str | None:
    if not text:
        return None
    return _clip(" ".join(scrub_text(text).split()), ERROR_MAX)


# --------------------------------------------------------------------------
# Matching (expect_tool_args)
# --------------------------------------------------------------------------
def value_matches(actual: Any, expected: Any) -> bool:
    """Case-insensitive substring for an expected string, equality otherwise."""
    if isinstance(expected, str):
        if actual is None:
            return False
        text = actual if isinstance(actual, str) else json.dumps(actual, default=str,
                                                                  ensure_ascii=False)
        return expected.lower() in text.lower()
    return actual == expected


def args_match(args: Any, expected: dict) -> bool:
    value = normalize_args(args)
    if not isinstance(value, dict):
        return False
    return all(k in value and value_matches(value[k], v) for k, v in expected.items())


# --------------------------------------------------------------------------
# One-line views (CLI, failure reasons)
# --------------------------------------------------------------------------
def _short_value(value: Any, limit: int = 40) -> str:
    if isinstance(value, str):
        text = value if value and not re.search(r"\s", value) else json.dumps(value, ensure_ascii=False)
    else:
        text = json.dumps(value, default=str, ensure_ascii=False)
    return _clip(text, limit)


def key_args(args: dict | None, *, limit: int = 4) -> str:
    """`name=refund-policy query="refund window"`: the first few arguments."""
    if not args:
        return ""
    items = list(args.items())
    out = [f"{k}={_short_value(v)}" for k, v in items[:limit]]
    if len(items) > limit:
        out.append("…")
    return " ".join(out)


def describe(call, *, turn: bool = False) -> str:
    """`read_knowledge name=refund-policy  ok  120 ms` (`call` is a
    ToolCallRecord or anything with the same attributes)."""
    head = call.name
    shown = key_args(call.args)
    if shown:
        head += " " + shown
    parts = [head]
    if call.ok is True:
        parts.append("ok")
    elif call.ok is False:
        parts.append("failed" + (f": {_clip(call.error, 120)}" if call.error else ""))
    if call.duration_ms is not None:
        parts.append(f"{call.duration_ms} ms")
    line = "  ".join(parts)
    return f"turn {call.turn}  {line}" if turn else line


def expected_text(expected: dict) -> str:
    """`name containing "refund-policy", limit=5` for a failure reason."""
    bits = []
    for k, v in expected.items():
        if isinstance(v, str):
            bits.append(f'{k} containing "{v}"')
        else:
            bits.append(f"{k}={json.dumps(v, default=str)}")
    return ", ".join(bits)
