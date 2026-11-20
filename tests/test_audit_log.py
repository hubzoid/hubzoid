"""Security audit events (HUBZOID_AUDIT) and the JSON log format
(HUBZOID_LOG_FORMAT): one redacted record per tool call, sign-in and admin
change, to the log, the database or both, and the export."""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from agents import function_tool
from agents.tool_context import ToolContext

from hubzoid import access, audit_log, logs
from hubzoid.access.identity import Identity, identity_scope


@function_tool
def read_file(path: str, note: str = "") -> str:
    """A tool whose path is an identifier and whose note is free text."""
    return f"read {path}"


def _invoke(tool, **kwargs):
    args = json.dumps(kwargs)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args)
    return asyncio.run(tool.on_invoke_tool(ctx, args))


@pytest.fixture
def audit_lines(monkeypatch):
    """The audit records written to the log, as dicts."""
    records = []

    class Catch(logging.Handler):
        def emit(self, record):
            records.append(json.loads(logs.AuditLineFormatter().format(record)))

    handler = Catch()
    logger = logging.getLogger("hubzoid.audit")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield records
    logger.removeHandler(handler)


def test_off_by_default_records_nothing(tmp_path, monkeypatch, audit_lines):
    monkeypatch.delenv("HUBZOID_AUDIT", raising=False)
    tool = access.apply(tmp_path, {"read_file": read_file})["read_file"]
    assert _invoke(tool, path="a.md") == "read a.md"
    assert audit_lines == []


def test_a_tool_call_is_one_redacted_record_in_the_log_and_the_table(tmp_path, monkeypatch, audit_lines):
    from hubzoid.access import evaluator

    monkeypatch.setenv("HUBZOID_AUDIT", "both")
    monkeypatch.setattr(evaluator, "check_run", lambda *a, **k: evaluator.Decision(True, "member"))
    tool = access.apply(tmp_path, {"read_file": read_file})["read_file"]
    with identity_scope(Identity.make("priya@example.com", [], surface="web")):
        _invoke(tool, path="https://drive.example.com/doc?token=abc", note="call me at priya@example.com")
    (rec,) = audit_lines
    assert rec["kind"] == "tool" and rec["event"] == "tool.call"
    assert rec["tool"] == "read_file" and rec["decision"] == "allow" and rec["outcome"] == "ok"
    assert rec["subject"] == "priya@example.com" and rec["surface"] == "web"
    assert rec["hub"] == tmp_path.name.lower()
    # The identifier is kept without its query; free text is not kept.
    assert rec["args"] == {"path": "https://drive.example.com/doc", "note": "…"}
    assert isinstance(rec["duration_ms"], int) and rec["result_bytes"] > 0
    audit_log.flush()
    (row,) = audit_log.read_events(tmp_path, kinds=("tool",))
    assert row["tool"] == "read_file" and row["subject"] == "priya@example.com"
    assert row["args"]["path"] == "https://drive.example.com/doc"


def test_a_refused_call_is_recorded_as_denied(tmp_path, monkeypatch, audit_lines):
    from hubzoid.access import evaluator

    monkeypatch.setenv("HUBZOID_AUDIT", "log")
    tool = access.apply(tmp_path, {"read_file": read_file})["read_file"]
    refused = evaluator.Decision(False, "credentials_revoked")
    monkeypatch.setattr(evaluator, "check_run", lambda *a, **k: refused)
    assert "access ended" in _invoke(tool, path="a.md")
    (rec,) = audit_lines
    assert rec["decision"] == "deny" and rec["reason"] == "credentials_revoked"
    assert "outcome" not in rec


def test_redacted_arguments_mask_secrets_and_personal_data(monkeypatch):
    monkeypatch.setenv("HUBZOID_AUDIT_ARGS", "redacted")
    out = audit_log.summarize_args({"to": "priya@example.com", "api_key": "sk-ant-abcdefghijklmnop",
                                    "body": "card 4111 1111 1111 1111"})
    assert "priya@example.com" not in out and "[email]" in out
    assert "sk-ant" not in out and "4111" not in out


def test_sign_in_failures_and_lockouts_are_auth_events(tmp_path, monkeypatch, audit_lines):
    from hubzoid.auth import ratelimit

    monkeypatch.setenv("HUBZOID_AUDIT", "both")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(tmp_path))
    locked = 0
    for _ in range(ratelimit.max_failures()):
        ratelimit.admit(tmp_path, ip="203.0.113.9", email="a@example.com")
        locked = ratelimit.record_failure(tmp_path, ip="203.0.113.9", email="a@example.com")
    assert locked
    events = [r["event"] for r in audit_lines]
    assert events.count("auth.sign_in_failed") == ratelimit.max_failures()
    assert events[-1] == "auth.locked_out"
    assert audit_lines[-1]["client"] == "203.0.113.9" and audit_lines[-1]["actor"] == "a@example.com"


def test_admin_changes_are_log_lines_and_sign_in_actions_are_auth(tmp_path, monkeypatch, audit_lines):
    monkeypatch.setenv("HUBZOID_AUDIT", "log")
    store = access.store_for(tmp_path)
    store.audit_event("admin@example.com", "grant", subject="priya@example.com", hub="sales", permission="erp")
    store.audit_event("priya@example.com", "signed_in", subject="priya@example.com", hub="*", permission="password")
    assert [r["event"] for r in audit_lines] == ["admin.grant", "auth.signed_in"]
    assert audit_lines[0]["actor"] == "admin@example.com" and audit_lines[0]["target_hub"] == "sales"


def test_export_merges_every_kind_oldest_first(tmp_path, monkeypatch):
    from hubzoid.access import audit as decisions

    monkeypatch.setenv("HUBZOID_AUDIT", "db")
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(tmp_path))
    decisions.record(tmp_path, user="p", surface="web", tool="erp", decision="deny", reason="no-grant")
    access.store_for(tmp_path).audit_event("admin@example.com", "grant", subject="p", hub=tmp_path.name.lower(),
                                           permission="erp")
    audit_log.tool_call(tool="read_file", hub_dir=tmp_path, outcome="ok", duration_ms=3)
    audit_log.sign_in_failed(email="p", client="198.51.100.1", locked_seconds=0)
    audit_log.flush()
    records = list(audit_log.export(tmp_path, hubs=[tmp_path.name.lower()]))
    assert {r["kind"] for r in records} == {"access", "admin", "tool", "auth"}
    assert [r["ts"] for r in records] == sorted(r["ts"] for r in records)
    assert [r["kind"] for r in audit_log.export(tmp_path, kinds=("tool",))] == ["tool"]
    assert list(audit_log.export(tmp_path, since="2999-01-01")) == []
    with pytest.raises(ValueError):
        list(audit_log.export(tmp_path, since="last tuesday"))


def test_json_log_lines_carry_the_request_context(monkeypatch):
    from hubzoid import _request_ctx

    record = logging.LogRecord("hubzoid.chat", logging.INFO, __file__, 1,
                               "reply for %s with Bearer abcdefghijklmnopqrstu", ("priya",), None)
    monkeypatch.setenv("HUBZOID_HUB_DIR", "/hubs/Sales")
    token = _request_ctx.set_client("203.0.113.7")
    try:
        with identity_scope(Identity.make("priya@example.com", [], surface="slack")), \
                _request_ctx.chat_scope("chat-1"):
            line = json.loads(logs.JsonFormatter().format(record))
    finally:
        _request_ctx._current_client.reset(token)
    assert line["level"] == "info" and line["logger"] == "hubzoid.chat"
    assert line["hub"] == "sales" and line["subject"] == "priya@example.com" and line["surface"] == "slack"
    assert line["chat_id"] == "chat-1" and line["client"] == "203.0.113.7"
    assert "abcdefghijklmnopqrstu" not in line["message"]
    assert logs.log_format({"HUBZOID_LOG_FORMAT": "JSON"}) == "json"
    assert logs.log_format({}) == "text"


def test_a_tool_call_makes_an_execute_tool_span_when_tracing_is_set_up(monkeypatch):
    spans = []

    class Span:
        def __init__(self, name, attributes):
            self.name, self.attributes, self.status = name, attributes, None

        def set_status(self, status):
            self.status = status

        def end(self, end_time=None):
            spans.append(self)

    class Tracer:
        def start_span(self, name, start_time=None, attributes=None):
            return Span(name, attributes)

    monkeypatch.delenv("HUBZOID_AUDIT", raising=False)
    monkeypatch.setattr(audit_log, "_tracer", lambda: Tracer())
    audit_log.tool_call(tool="read_file", outcome="ok", duration_ms=12, call_id="c1", arguments={"path": "x"})
    (span,) = spans
    assert span.name == "execute_tool read_file"
    assert span.attributes["gen_ai.operation.name"] == "execute_tool"
    assert span.attributes["gen_ai.tool.call.id"] == "c1"
    assert "path" not in json.dumps(span.attributes)   # never arguments
