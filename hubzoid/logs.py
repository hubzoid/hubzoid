"""How Hubzoid's processes write their log lines.

``HUBZOID_LOG_FORMAT=text`` (the default) keeps today's lines.
``HUBZOID_LOG_FORMAT=json`` writes one JSON object per line on standard
output, for CloudWatch, Datadog, Loki or a SIEM: time, level, logger, message,
and what is known about the request it belongs to (hub, surface, person, chat,
workflow run, trace). Messages pass the shared credential scrub
(``hubzoid.redaction``) in either format.

Audit events (``hubzoid.audit``) always reach the output when they are on
(``HUBZOID_AUDIT=log``), as one JSON object per line, whatever the format.

``configure`` is called once by each process that serves requests (the
bridge, the edge, the inbound server). It is idempotent. Standard library
only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

FORMAT_ENV = "HUBZOID_LOG_FORMAT"
_CONFIGURED: dict[str, Any] = {}
# What ``LogRecord`` carries by itself: everything else on a record is an
# ``extra`` a caller added.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


def log_format(env=None) -> str:
    env = os.environ if env is None else env
    value = (env.get(FORMAT_ENV) or "").strip().lower()
    return "json" if value == "json" else "text"


def context() -> dict[str, Any]:
    """What is known about the work this line belongs to. Never raises."""
    out: dict[str, Any] = {}
    hub = (os.environ.get("HUBZOID_HUB_DIR") or "").strip()
    if hub:
        out["hub"] = os.path.basename(hub.rstrip("/\\")).lower()
    try:
        from .access.identity import current_identity

        ident = current_identity()
        if ident.user:
            out["subject"] = ident.user
        if ident.surface and ident.surface != "system":
            out["surface"] = ident.surface
    except Exception:  # noqa: BLE001 - context is best effort
        pass
    try:
        from .workflows.context import _run

        run = _run.get()
        if run is not None:
            out.setdefault("subject", run.subject or f"workflow:{run.workflow}")
            out["run_id"] = getattr(run, "run_id", None)
            out["workflow"] = getattr(run, "workflow", None)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import _request_ctx

        chat = _request_ctx.get_chat_id()
        if chat:
            out["chat_id"] = chat
        turn = _request_ctx.get_turn_id()
        if turn:
            out["turn_id"] = turn
        client = _request_ctx.get_client()
        if client:
            out["client"] = client
    except Exception:  # noqa: BLE001
        pass
    trace = _trace_id()
    if trace:
        out["trace_id"] = trace
    return {k: v for k, v in out.items() if v not in (None, "")}


def _trace_id() -> str | None:
    """The active OpenTelemetry trace, when the OTel API is installed and a
    span is recording."""
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    try:
        span = trace.get_current_span().get_span_context()
        return format(span.trace_id, "032x") if span.is_valid else None
    except Exception:  # noqa: BLE001
        return None


def _scrub(text: str) -> str:
    from .redaction import scrub_text

    try:
        return scrub_text(text)
    except Exception:  # noqa: BLE001 - a log line is never lost to the scrub
        return text


class JsonFormatter(logging.Formatter):
    """One JSON object per record: time (UTC, ISO), level, logger, message,
    the request context and any ``extra`` fields. An audit record's fields
    are its own (``hz_audit``)."""

    def format(self, record: logging.LogRecord) -> str:
        audit = getattr(record, "hz_audit", None)
        if isinstance(audit, dict):
            return json.dumps(audit, default=str, ensure_ascii=False)
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": _scrub(record.getMessage()),
        }
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, _version, status = record.args
            out.update(client=client, method=method, path=_scrub(str(path)), status=status)
        out.update(context())
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_") and key not in out and key != "color_message":
                out[key] = value
        if record.exc_info:
            out["error"] = _scrub(self.formatException(record.exc_info))
        return json.dumps(out, default=str, ensure_ascii=False)


class AuditLineFormatter(logging.Formatter):
    """Audit records in the text format: the record itself, as one JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        audit = getattr(record, "hz_audit", None)
        if isinstance(audit, dict):
            return json.dumps(audit, default=str, ensure_ascii=False)
        return _scrub(record.getMessage())


class RedactMessages(logging.Filter):
    """The credential scrub on every message and string argument."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(_scrub(a) if isinstance(a, str) else a for a in record.args)
        return True


def configure(env=None) -> str:
    """Set up this process's log output once. Returns the format in use."""
    env = os.environ if env is None else env
    fmt = log_format(env)
    if _CONFIGURED.get("format") == fmt:
        return fmt
    level = (env.get("HUB_LOG_LEVEL") or "info").strip().upper()
    hubzoid_logger = logging.getLogger("hubzoid")
    if hubzoid_logger.level == logging.NOTSET:
        hubzoid_logger.setLevel(getattr(logging, level, logging.INFO))
    audit = logging.getLogger("hubzoid.audit")
    audit.setLevel(logging.INFO)
    if fmt == "json":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RedactMessages())
        root = logging.getLogger()
        root.handlers = [h for h in root.handlers if not getattr(h, "hubzoid", False)]
        handler.hubzoid = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        # uvicorn wires its own loggers: same format there.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            for h in logging.getLogger(name).handlers:
                h.setFormatter(JsonFormatter())
        audit.propagate = True
        for h in list(audit.handlers):
            if getattr(h, "hubzoid", False):
                audit.removeHandler(h)
    else:
        if not any(getattr(h, "hubzoid", False) for h in audit.handlers):
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(AuditLineFormatter())
            handler.hubzoid = True  # type: ignore[attr-defined]
            audit.addHandler(handler)
        audit.propagate = False  # one line per event, not one per handler up the tree
    _CONFIGURED["format"] = fmt
    return fmt
