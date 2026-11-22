"""Security audit events: every tool call, sign-in and admin change, as one
structured, redacted record a security team can forward and alert on.

``HUBZOID_AUDIT`` chooses where records go:

* ``off`` (default): nothing new. Restricted-tool decisions and admin changes
  keep their own tables, as before.
* ``log``: one JSON line per event on the ``hubzoid.audit.<kind>`` logger, on
  standard output (``hubzoid.logs``), so ECS sends it to CloudWatch and a
  forwarder takes it to the SIEM.
* ``db``: a row in ``hz_audit_events`` (tool calls and sign-in events), shown
  on the Console's Activity page and in ``hubzoid audit export``. Kept
  ``HUBZOID_AUDIT_RETENTION_DAYS`` days (default 90).
* ``both``: the two together.

``HUBZOID_AUDIT_ARGS`` says what a tool call record keeps of its arguments:
``names`` (default: argument names, plus identifiers such as paths, ids and
web addresses without their query) or ``redacted`` (values after the
credential scrub, with email addresses, phone numbers and card numbers
masked). Every record passes the shared scrub (``hubzoid.redaction``).

Records never raise into the caller and never slow a tool call: database rows
are queued and written in batches by one background thread.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

log = logging.getLogger("hubzoid.audit")

MODE_ENV = "HUBZOID_AUDIT"
ARGS_ENV = "HUBZOID_AUDIT_ARGS"
RETENTION_ENV = "HUBZOID_AUDIT_RETENTION_DAYS"
KINDS = ("tool", "auth", "admin", "access")
_COLUMNS = ("ts", "kind", "event", "hub", "subject", "surface", "tool", "server", "decision", "outcome",
            "duration_ms", "client", "chat_id", "run_id")


def modes(env=None) -> frozenset[str]:
    """The sinks that are on: a subset of {"log", "db"}."""
    env = os.environ if env is None else env
    value = (env.get(MODE_ENV) or "").strip().lower()
    return {"log": frozenset({"log"}), "db": frozenset({"db"}),
            "both": frozenset({"log", "db"})}.get(value, frozenset())


def enabled(env=None) -> bool:
    return bool(modes(env))


def retention_days(env=None) -> int:
    env = os.environ if env is None else env
    try:
        return max(1, int((env.get(RETENTION_ENV) or "").strip() or 90))
    except ValueError:
        return 90


# -- argument summaries ---------------------------------------------------------------------

# Argument names whose value says what was touched, not what was said.
_IDENTIFIER_NAMES = frozenset({"path", "file", "filename", "file_path", "filepath", "dir", "directory",
                               "folder", "id", "url", "uri", "link", "repo", "repository", "branch",
                               "table", "collection", "channel", "sheet", "page", "workflow", "name",
                               "slug", "ref", "issue", "ticket", "project"})
_KEEP = 200
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<![\w])\+?\d[\d ()-]{7,}\d")


def _identifier(name: str) -> bool:
    n = name.lower()
    return n in _IDENTIFIER_NAMES or n.endswith(("_id", "_path", "_url", "_file", "_name"))


def _clip(value: Any, limit: int = _KEEP) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def _without_query(value: str) -> str:
    return value.split("?", 1)[0].split("#", 1)[0] if "://" in value else value


def _mask_personal(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _CARD.sub("[card]", text)
    return _PHONE.sub("[phone]", text)


def summarize_args(arguments: Any, env=None) -> Any:
    """What a record keeps of a tool call's arguments (``HUBZOID_AUDIT_ARGS``)."""
    from .redaction import is_secret_key, redact

    env = os.environ if env is None else env
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            arguments = {"input": arguments}
    if not isinstance(arguments, dict):
        return None if arguments is None else {"input": "…"}
    mode = (env.get(ARGS_ENV) or "names").strip().lower()
    if mode == "redacted":
        # A JSON text: the values after the scrub, personal data masked, capped.
        return _clip(_mask_personal(json.dumps(redact(arguments), default=str, ensure_ascii=False)), 1000)
    out: dict[str, Any] = {}
    for name, value in arguments.items():
        if _identifier(str(name)) and not is_secret_key(name) and isinstance(value, (str, int, float)):
            # The whole value is scrubbed first, so clipping can't cut a
            # credential's end off and hide it from the scrub.
            out[str(name)] = _clip(_without_query(redact(str(value))))
        else:
            out[str(name)] = "…"
    return redact(out)


# -- the database sink ------------------------------------------------------------------------

class _Writer:
    """One background thread per process writing queued rows in batches, and
    removing rows past their retention now and then."""

    def __init__(self) -> None:
        self.q: "queue.Queue[tuple[Path, dict]]" = queue.Queue(maxsize=10000)
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.pruned: dict[str, float] = {}
        self.dropped = 0

    def put(self, hub_dir: Path, row: dict) -> None:
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                if self.thread is None:
                    import atexit

                    # A graceful stop (ECS sends SIGTERM) writes what is still queued.
                    atexit.register(self.flush)
                self.thread = threading.Thread(target=self._run, name="hubzoid-audit", daemon=True)
                self.thread.start()
        try:
            self.q.put_nowait((hub_dir, row))
        except queue.Full:
            self.dropped += 1
            if self.dropped in (1, 100) or self.dropped % 1000 == 0:
                log.error("audit: %d record(s) not written to the database (queue full)", self.dropped)

    def _run(self) -> None:
        while True:
            first = self.q.get()
            batch = [first]
            deadline = time.monotonic() + 0.5
            while len(batch) < 200 and time.monotonic() < deadline:
                try:
                    batch.append(self.q.get(timeout=max(0.0, deadline - time.monotonic())))
                except queue.Empty:
                    break
            try:
                self.write(batch)
            finally:
                for _ in batch:
                    self.q.task_done()

    def write(self, batch: list[tuple[Path, dict]]) -> None:
        from sqlalchemy import text

        by_hub: dict[str, tuple[Path, list[dict]]] = {}
        for hub_dir, row in batch:
            by_hub.setdefault(str(hub_dir), (hub_dir, []))[1].append(row)
        for key, (hub_dir, rows) in by_hub.items():
            try:
                engine = _engine(hub_dir)
                with engine.begin() as c:
                    c.execute(text(f"INSERT INTO hz_audit_events ({', '.join(_COLUMNS)}, detail) VALUES "
                                   f"({', '.join(':' + col for col in _COLUMNS)}, :detail)"), rows)
                    if time.time() - self.pruned.get(key, 0.0) > 3600:
                        self.pruned[key] = time.time()
                        c.execute(text("DELETE FROM hz_audit_events WHERE ts < :cut"),
                                  {"cut": time.time() - retention_days() * 86400})
            except Exception:  # noqa: BLE001 - the log sink and the caller carry on
                log.error("audit: %d record(s) could not be written to the database", len(rows), exc_info=True)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until everything queued so far is written (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        while self.q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)


_WRITER = _Writer()


def flush() -> None:
    _WRITER.flush()


def _engine(hub_dir: Path):
    from . import db, migrations

    engine = db.operational_engine(hub_dir)
    migrations.upgrade(engine, "operational")
    return engine


# -- events ---------------------------------------------------------------------------------

def _now_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def _hub_dir(hub_dir) -> Path | None:
    if hub_dir:
        return Path(hub_dir)
    raw = (os.environ.get("HUBZOID_HUB_DIR") or "").strip()
    return Path(raw) if raw else None


def emit(kind: str, event: str, *, hub_dir=None, store: bool = True, **fields: Any) -> None:
    """Record one audit event in the sinks that are on. Never raises."""
    sinks = modes()
    if not sinks:
        return
    try:
        from .logs import context
        from .redaction import redact

        ts = time.time()
        hd = _hub_dir(hub_dir)
        record: dict[str, Any] = {"ts": _now_iso(ts), "kind": kind, "event": event}
        ctx = context()
        if hd is not None:
            ctx["hub"] = hd.name.lower()
        record.update(ctx)
        record.update({k: v for k, v in fields.items() if v is not None and v != ""})
        record = redact(record)
        if "log" in sinks:
            logging.getLogger(f"hubzoid.audit.{kind}").info(event, extra={"hz_audit": record})
        if "db" in sinks and store and hd is not None and kind in ("tool", "auth"):
            row = {col: record.get(col) for col in _COLUMNS}
            row["ts"] = ts
            if row.get("duration_ms") is not None:
                row["duration_ms"] = int(row["duration_ms"])
            detail = {k: v for k, v in record.items() if k not in _COLUMNS}
            row["detail"] = json.dumps(detail, default=str, ensure_ascii=False) if detail else None
            _WRITER.put(hd, row)
    except Exception:  # noqa: BLE001 - an audit failure never breaks the work
        log.error("audit: could not record %s", event, exc_info=True)


# The outermost tool wrapper on a call path records the call; wrappers inside
# it (an approval step wraps the tool and checks it again) only note a refusal
# for it. A dict shared by reference, so a copied context still reaches it.
_CALL: ContextVar["dict | None"] = ContextVar("hubzoid_audit_call", default=None)


@contextmanager
def call_scope():
    """``(state, outermost)`` for one tool call: record only when outermost."""
    state = _CALL.get()
    if state is not None:
        yield state, False
        return
    state = {}
    token = _CALL.set(state)
    try:
        yield state, True
    finally:
        _CALL.reset(token)


@contextmanager
def separate_calls():
    """A nested agent run inside one tool call (an MCP sub-agent handover):
    the tool calls it makes are calls of their own, recorded on their own,
    not wrappers around the call that started it."""
    token = _CALL.set(None)
    try:
        yield
    finally:
        _CALL.reset(token)


def finished(where: dict, result: Any, *, started: float, arguments: Any, state: dict,
             failed: bool = False) -> None:
    """Record a call that returned: refused by a wrapper inside, refused in
    the tool's own words, failed (an error the tool runtime turned into text),
    or done."""
    from .tool_events import failed_output

    took = (time.monotonic() - started) * 1000
    if state.get("deny"):
        tool_call(**where, decision="deny", reason=state["deny"], arguments=arguments)
    elif refused_text(result):
        tool_call(**where, decision="deny", reason="permission", arguments=arguments)
    elif failed or failed_output(result):
        tool_call(**where, outcome="error", error="tool_error", duration_ms=took, result=result, arguments=arguments)
    else:
        tool_call(**where, outcome="ok", duration_ms=took, result=result, arguments=arguments)


def tool_call(*, tool: str, hub_dir=None, server: str | None = None, connector: str | None = None,
              decision: str = "allow", reason: str | None = None, outcome: str | None = None,
              error: str | None = None, duration_ms: float | None = None, result: Any = None,
              arguments: Any = None, call_id: str | None = None) -> None:
    """One tool call: who called which tool, from where, allowed or refused,
    how it ended, how long it took. Also an OpenTelemetry ``execute_tool``
    span when a tracer is set up (``opentelemetry-instrument``)."""
    tracer = _tracer()
    if tracer is not None:
        _tool_span(tracer, tool=tool, server=server or connector, decision=decision, reason=reason,
                   error=error, duration_ms=duration_ms, call_id=call_id)
    if not modes():
        return
    try:
        args = summarize_args(arguments)
    except Exception:  # noqa: BLE001
        args = None
    emit("tool", "tool.call", hub_dir=hub_dir, tool=tool, server=server or (f"connector:{connector}"
         if connector else None), decision=decision, reason=reason, outcome=outcome, error=error,
         duration_ms=None if duration_ms is None else round(duration_ms),
         result_bytes=None if outcome is None else size_of(result), args=args, call_id=call_id)


_PENDING: "weakref.WeakKeyDictionary" = None  # type: ignore[assignment]


def after_commit(conn, fn) -> None:
    """Run ``fn`` once ``conn``'s transaction commits, and drop it on a
    rollback, so a log line never describes a change that did not happen."""
    global _PENDING

    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Connection
    except ImportError:  # pragma: no cover
        fn()
        return
    if not isinstance(conn, Connection) or not conn.in_transaction():
        fn()
        return
    if _PENDING is None:
        _PENDING = weakref.WeakKeyDictionary()
    pending = _PENDING.get(conn)
    if pending is None:
        pending = _PENDING[conn] = []

        def on_commit(c) -> None:
            for f in (_PENDING.pop(c, None) or []):
                try:
                    f()
                except Exception:  # noqa: BLE001
                    log.error("audit: could not record a committed change", exc_info=True)

        def on_rollback(c) -> None:
            _PENDING.pop(c, None)

        event.listen(conn, "commit", on_commit)
        event.listen(conn, "rollback", on_rollback)
    pending.append(fn)


# Actions in the shared admin audit that are about signing in, not changing
# what people may do.
_AUTH_ACTIONS = ("signed_", "api_key_", "mfa_", "password_", "session", "sign_in", "signin")


def admin_event(actor: str | None, action: str, *, subject: str | None = None, target_hub: str | None = None,
                detail: str | None = None, via: str | None = None, conn=None) -> None:
    """A row the deployment's admin audit (``hz_access_audit``) just got, as
    an event: ``auth.*`` for sign-in and API key actions, ``admin.*`` for the
    rest (grants, secrets, run controls). Never a secret value: the rows hold
    names and summaries only."""
    if not modes():
        return
    kind = "auth" if str(action).startswith(_AUTH_ACTIONS) else "admin"
    from .logs import context

    ctx = context()  # captured now: the commit may run after this request's context ends

    def send() -> None:
        # The row is already stored (hz_access_audit): a log line only.
        emit(kind, f"{kind}.{action}", actor=actor, target=subject, target_hub=target_hub, detail=detail,
             via=via, store=False, **{k: v for k, v in ctx.items() if k in ("client", "chat_id", "run_id")})

    if conn is None:
        send()
    else:
        after_commit(conn, send)


_KEY_ADDRESSES: set[tuple[str, str]] = set()


def api_key_used(key_id: str, email: str | None) -> None:
    """A personal API key's first use from an address since this process
    started (a security team compares addresses across events)."""
    if not modes():
        return
    from . import _request_ctx

    client = _request_ctx.get_client() or ""
    if (key_id, client) in _KEY_ADDRESSES or len(_KEY_ADDRESSES) > 50000:
        return
    _KEY_ADDRESSES.add((key_id, client))
    emit("auth", "auth.api_key_used", key=str(key_id)[:16], actor=email, subject=email)


def app_token_used(app_id: str, email: str | None) -> None:
    """A machine JWT from a registered API app (``apps:`` with ``api:``) used
    from an address for the first time since this process started."""
    if not modes():
        return
    from . import _request_ctx

    client = _request_ctx.get_client() or ""
    key = (f"app:{app_id}", client)
    if key in _KEY_ADDRESSES or len(_KEY_ADDRESSES) > 50000:
        return
    _KEY_ADDRESSES.add(key)
    emit("auth", "auth.app_token_used", key=f"app:{app_id}"[:44], actor=email, subject=email)


def sign_in_failed(*, email: str | None, client: str | None, locked_seconds: int) -> None:
    """A failed sign-in step (password, code, re-check), and a lockout when it
    reached the limit."""
    if not modes():
        return
    # The account tried is the event's subject, so the Activity filter by person finds it.
    # Every counted failure (unknown accounts, codes, re-checks too).
    # auth.sign_in_failed comes from the sign-in route for a known account.
    emit("auth", "auth.attempt_failed", actor=email, subject=email, client=client)
    if locked_seconds:
        emit("auth", "auth.locked_out", actor=email, subject=email, client=client,
             locked_seconds=int(locked_seconds))


def _tracer():
    """Hubzoid's tracer when the OpenTelemetry API is installed and a real
    tracer provider is set (for example by ``opentelemetry-instrument``),
    else None. Spans are never made into the no-op provider."""
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    try:
        provider = trace.get_tracer_provider()
        if type(provider).__name__ in ("ProxyTracerProvider", "NoOpTracerProvider"):
            return None
        return trace.get_tracer("hubzoid")
    except Exception:  # noqa: BLE001
        return None


def _tool_span(tracer, *, tool, server, decision, reason, error, duration_ms, call_id) -> None:
    """The GenAI ``execute_tool`` span (the names Mastra and eve use), with
    who and where as hubzoid.* attributes. Never tool arguments or results."""
    try:
        from .logs import context

        end = time.time_ns()
        start = end - int((duration_ms or 0) * 1_000_000)
        ctx = context()
        attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
                 "hubzoid.decision": decision}
        for key, value in (("gen_ai.tool.call.id", call_id), ("error.type", error or (reason if decision == "deny"
                                                                                       else None)),
                           ("hubzoid.server", server), ("hubzoid.hub", ctx.get("hub")),
                           ("hubzoid.surface", ctx.get("surface")), ("hubzoid.user", ctx.get("subject")),
                           ("hubzoid.chat_id", ctx.get("chat_id"))):
            if value:
                attrs[key] = str(value)
        span = tracer.start_span(f"execute_tool {tool}", start_time=start, attributes=attrs)
        if error or decision == "deny":
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, error or reason or "denied"))
        span.end(end_time=end)
    except Exception:  # noqa: BLE001 - telemetry never breaks the call
        log.debug("audit: tool span failed", exc_info=True)


def size_of(result: Any) -> int | None:
    """A tool result's size in bytes, roughly (text as UTF-8, anything else as JSON)."""
    try:
        if result is None:
            return None
        if isinstance(result, (bytes, bytearray)):
            return len(result)
        if not isinstance(result, str):
            content = getattr(result, "content", None)
            if content is not None:
                result = [getattr(p, "text", "") for p in content]
            result = json.dumps(result, default=str, ensure_ascii=False)
        return len(result.encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def refused_text(result: Any) -> bool:
    """Whether a hub tool's answer is one of Hubzoid's own refusals."""
    return isinstance(result, str) and result.startswith(("[access denied", "[access ended"))


# -- reading and export ---------------------------------------------------------------------

def _when(value) -> float | None:
    """An instant from ISO 8601 (naive means UTC) or epoch seconds, else None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    from .access.audit import _instant

    when = _instant(str(value))
    if when is None:
        raise ValueError(f"not a time: {value!r} (use ISO 8601, for example 2026-11-20T09:00:00Z)")
    return when.timestamp()


def _iso(ts: float) -> str:
    return _now_iso(float(ts))


def _event_row(r) -> dict:
    m = dict(r._mapping)
    detail = {}
    if m.get("detail"):
        try:
            detail = json.loads(m["detail"])
        except ValueError:
            detail = {}
    out = {"ts": _iso(m["ts"]), "kind": m["kind"], "event": m["event"], "hub": m.get("hub")}
    for col in ("subject", "surface", "tool", "server", "decision", "outcome", "duration_ms", "client",
                "chat_id", "run_id"):
        if m.get(col) is not None:
            out[col] = m[col]
    for key, value in detail.items():
        out.setdefault(key, value)
    return out


def _where(clauses: list[str], params: dict, *, hubs, since, until, column_hub="hub") -> None:
    if hubs is not None:
        names = []
        for i, h in enumerate(hubs):
            params[f"h{i}"] = h
            names.append(f":h{i}")
        clauses.append(f"{column_hub} IN (" + ",".join(names or ["NULL"]) + ")")
    if since is not None:
        clauses.append("ts >= :since")
        params["since"] = since
    if until is not None:
        clauses.append("ts <= :until")
        params["until"] = until


def read_events(hub_dir, *, kinds=("tool", "auth"), limit: int = 200, user: str | None = None,
                decision: str | None = None, outcome: str | None = None, tool: str | None = None,
                surface: str | None = None, since=None, until=None) -> list[dict]:
    """The most recent tool and sign-in events for this hub, newest first,
    with filters applied before the limit (the Console's Activity page)."""
    from sqlalchemy import text

    from .access.identity import normalize

    hub = normalize(Path(hub_dir).name)
    clauses = ["kind IN (" + ",".join(f":k{i}" for i, _ in enumerate(kinds)) + ")"]
    params: dict[str, Any] = {f"k{i}": k for i, k in enumerate(kinds)}
    _where(clauses, params, hubs=[hub], since=_when(since), until=_when(until))
    for col, value in (("subject", normalize(user) if user else None), ("decision", decision),
                       ("outcome", outcome), ("tool", tool), ("surface", surface)):
        if value:
            clauses.append(f"{col} = :{col}")
            params[col] = value
    params["n"] = int(limit)
    q = ("SELECT * FROM hz_audit_events WHERE " + " AND ".join(clauses)
         + " ORDER BY ts DESC, id DESC LIMIT :n")
    with _engine(Path(hub_dir)).connect() as c:
        return [_event_row(r) for r in c.execute(text(q), params)]


def _auth_action_sql() -> str:
    """SQL for "this hz_access_audit action is a sign-in action"."""
    return "(" + " OR ".join(f"action LIKE '{p}%'" for p in _AUTH_ACTIONS) + ")"


def export(hub_dir, *, kinds=KINDS, since=None, until=None, hubs: list[str] | None = None,
           limit: int = 100000):
    """Every stored audit record in the window, oldest first, as the same JSON
    records the log lines carry: tool calls and sign-in events
    (``hz_audit_events``), restricted-tool decisions (``hz_access_decisions``)
    and admin changes (``hz_access_audit``). ``hubs`` limits it to those hubs
    (admin changes for the whole organization count as hub ``*``). Yields
    dicts, at most ``limit`` in all. When more remain, a last record
    ``{"kind": "export", "event": "export.truncated", "next_since": ...}``
    says where to continue (records at that instant may repeat)."""
    from sqlalchemy import text

    since, until = _when(since), _when(until)
    kinds = tuple(k for k in kinds if k in KINDS)
    streams = []
    engine = _engine(Path(hub_dir))
    with engine.connect() as c:
        if {"tool", "auth"} & set(kinds):
            want = [k for k in ("tool", "auth") if k in kinds]
            clauses = ["kind IN (" + ",".join(f":k{i}" for i, _ in enumerate(want)) + ")"]
            params: dict[str, Any] = {f"k{i}": k for i, k in enumerate(want)}
            _where(clauses, params, hubs=hubs, since=since, until=until)
            params["n"] = limit + 1
            streams.append([_event_row(r) for r in c.execute(text(
                "SELECT * FROM hz_audit_events WHERE " + " AND ".join(clauses) + " ORDER BY ts, id LIMIT :n"),
                params)])
        if "access" in kinds:
            clauses, params = [], {"n": limit + 1}
            _where(clauses, params, hubs=hubs, since=since, until=until)
            rows = c.execute(text(
                "SELECT ts, hub, subject, surface, tool, decision, reason FROM hz_access_decisions"
                + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY ts, id LIMIT :n"), params)
            streams.append([{"ts": _iso(r[0]), "kind": "access", "event": "access.decision", "hub": r[1],
                             "subject": r[2], "surface": r[3], "tool": r[4], "decision": r[5],
                             "reason": r[6]} for r in rows])
        if {"admin", "auth"} & set(kinds):
            clauses, params = [], {"n": limit + 1}
            _where(clauses, params, hubs=None if hubs is None else [*hubs, "*"], since=since, until=until)
            if "admin" not in kinds:
                clauses.append(_auth_action_sql())
            elif "auth" not in kinds:
                clauses.append("NOT " + _auth_action_sql())
            rows = c.execute(text(
                "SELECT ts, actor, action, subject, hub, permission, surface, request_id FROM hz_access_audit"
                + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY ts LIMIT :n"), params)
            admin_rows = []
            for r in rows:
                kind = "auth" if str(r[2]).startswith(_AUTH_ACTIONS) else "admin"
                if kind not in kinds:
                    continue
                admin_rows.append({k: v for k, v in {
                    "ts": _iso(r[0]), "kind": kind, "event": f"{kind}.{r[2]}", "actor": r[1], "target": r[3],
                    "target_hub": r[4], "detail": r[5], "via": r[6], "request_id": r[7]}.items()
                    if v is not None})
            streams.append(admin_rows)
    import heapq

    merged = heapq.merge(*streams, key=lambda rec: rec["ts"])
    for n, record in enumerate(merged):
        if n >= limit:
            yield {"kind": "export", "event": "export.truncated", "limit": limit, "next_since": record["ts"]}
            return
        yield record
