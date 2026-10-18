"""One credential scrub for everything Hubzoid stores or shows.

Tool results, operation errors and evidence, eval previews and access logs all
pass through the same rules, so a credential removed in one place is removed in
every place:

  * values under secret-looking keys (token, password, secret, api_key,
    authorization) in structured data;
  * ``key: value`` and ``key=value`` pairs with those words in free text;
  * ``Bearer``/``Basic`` credentials, credentials embedded in URLs
    (``https://user:token@host``) and well-known token shapes (GitHub, Slack,
    OpenAI, Anthropic, AWS access keys);
  * one-time sign-in values in URLs (``/api/auth/link/<token>``, ``token=``,
    ``code=``, ``state=`` query parameters);
  * any exact secret value the caller passes in ``secrets``.

Counts such as ``max_tokens`` or ``input_tokens`` are kept: they are not
secrets. Pure functions with no I/O, so every caller can use them.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

REDACTED = "[redacted]"

_SECRET_WORDS = ("token", "password", "passwd", "secret", "api_key", "apikey",
                 "authorization")

# `key: value`, `key=value` and `"key": "value"` in free text.
_SECRET_TEXT_RE = re.compile(
    r"""(?ix)
    (["']?[\w-]*(?:token|password|passwd|secret|api[_-]?key|authorization)[\w-]*["']?\s*[:=]\s*)
    ("[^"]*"|'[^']*'|[^\s,;}&]+)
    """)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
# https://user:secret@host and https://secret@host
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+(?::[^/\s@]*)?@")
# Token shapes that are credentials wherever they appear.
_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16})\b")
# One-time sign-in values in URLs and access-log lines.
_URL_PATTERNS = (
    (re.compile(r"(/api/auth/link/)[^/?#\s\"']+"), r"\1" + REDACTED),
    (re.compile(r"([?&](?:token|code|state)=)[^&#\s\"']+"), r"\1" + REDACTED),
)


def is_secret_key(key: object) -> bool:
    """True for a key whose value should never be stored (`max_tokens`, a
    count, is not a secret)."""
    k = str(key).strip().lower().replace("-", "_").replace(" ", "_")
    if k.endswith("tokens"):
        return False
    return any(word in k for word in _SECRET_WORDS)


def redact_url_credentials(text: str) -> str:
    """Only the sign-in values carried in URLs: links, tokens, OAuth codes."""
    for pattern, replacement in _URL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub_string(text: str, secrets: tuple[str, ...]) -> str:
    for value in secrets:
        text = text.replace(value, REDACTED)
    # Most specific first: a credential inside a URL or after "Bearer" is
    # removed before the generic `key: value` rule could keep its tail.
    text = _URL_CREDENTIAL_RE.sub(lambda m: m.group(1) + REDACTED + "@", text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + " " + REDACTED, text)
    text = _TOKEN_SHAPES.sub(REDACTED, text)
    text = _SECRET_TEXT_RE.sub(
        lambda m: m.group(0) if REDACTED in m.group(2) or m.group(2).lower() in ("bearer", "basic")
        else m.group(1) + REDACTED, text)
    return redact_url_credentials(text)


def _secrets(values: Iterable[str] | None) -> tuple[str, ...]:
    # Longest first, so a secret that contains another is replaced whole.
    return tuple(sorted({v for v in (values or ()) if v and len(v) >= 6}, key=len, reverse=True))


def redact(value: Any, *, secrets: Iterable[str] | None = None) -> Any:
    """`value` with secret-looking keys and credentials in strings replaced,
    recursively. Structure, numbers and other values are kept."""
    known = _secrets(secrets)

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {str(k): (REDACTED if is_secret_key(k) else walk(x)) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        if isinstance(v, str):
            return _scrub_string(v, known)
        return v

    return walk(value)


def scrub_text(text: str, *, secrets: Iterable[str] | None = None) -> str:
    """Free text with credentials redacted. A JSON payload is parsed and
    redacted by key instead, then written back as JSON."""
    if not isinstance(text, str):
        text = str(text)
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.dumps(redact(json.loads(stripped), secrets=secrets), ensure_ascii=False)
        except ValueError:
            pass
    return _scrub_string(text, _secrets(secrets))


def scrub_error(exc: BaseException, *, limit: int = 2000,
                secrets: Iterable[str] | None = None) -> str:
    """An exception as one safe line: its type and scrubbed message, bounded."""
    message = scrub_text(f"{type(exc).__name__}: {exc}", secrets=secrets)
    return message if len(message) <= limit else message[: limit - 1] + "…"
