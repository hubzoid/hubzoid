"""Keep one-time credentials out of access logs.

A set-password link (``/auth/set-password?token=...``), its API path
(``/api/auth/link/<token>``) and an OAuth callback (``?code=...&state=...``)
carry credentials in the URL, and uvicorn's access log records the path with
its query string. ``install`` adds a filter to the ``uvicorn.access`` logger
that replaces those values with ``[redacted]`` before any handler sees them.

The bridge installs it when the sign-in routes mount. Any other process that
serves these URLs (the edge, a reverse proxy) needs the same treatment; for a
proxy, redact the ``token``, ``code`` and ``state`` query parameters and the
``/api/auth/link/`` path segment in its log format.
"""
from __future__ import annotations

import logging
import re

_PATTERNS = (
    (re.compile(r"(/api/auth/link/)[^/?#\s\"']+"), r"\1[redacted]"),
    (re.compile(r"([?&](?:token|code|state)=)[^&#\s\"']+"), r"\1[redacted]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactCredentials(logging.Filter):
    """Redacts sign-in credentials in a log record's message and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def install(*names: str) -> None:
    """Add the filter to these loggers (default ``uvicorn.access``), once."""
    for name in names or ("uvicorn.access",):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactCredentials) for f in logger.filters):
            logger.addFilter(RedactCredentials())
