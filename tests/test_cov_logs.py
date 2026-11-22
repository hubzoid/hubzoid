"""JSON log lines (hubzoid.logs.JsonFormatter): each record carries its
request context (hub, person, surface, chat, workflow run), uvicorn access
fields, extras and the error, always through the credential scrub; audit
records are written as their own fields; a full audit queue drops lines and
says so instead of blocking."""
from __future__ import annotations

import json
import logging
import queue
import sys

from hubzoid import _request_ctx, access, db, logs
from hubzoid.workflows import context
from hubzoid.workflows.identity import RunIdentity

AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"  # assembled, so no key-shaped literal is stored


def record(msg, *args, name="hubzoid.test", exc_info=None, **extra):
    rec = logging.LogRecord(name, logging.WARNING, __file__, 1, msg, args, exc_info)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_format_choice():
    assert logs.log_format({"HUBZOID_LOG_FORMAT": " JSON "}) == "json"
    assert logs.log_format({"HUBZOID_LOG_FORMAT": "yaml"}) == "text"
    assert logs.log_format({}) == "text"


def test_a_line_carries_its_context_extras_and_a_scrubbed_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(tmp_path / "Sales-Hub"))
    fmt = logs.JsonFormatter()
    with access.identity_scope(access.Identity.make(user="ana@example.org", surface="slack")), \
            _request_ctx.chat_scope("chat-7"):
        line = json.loads(fmt.format(record("key %s used", AWS_KEY, order_id=42)))
    assert line["level"] == "warning" and line["logger"] == "hubzoid.test" and line["ts"].endswith("Z")
    assert line["hub"] == "sales-hub" and line["subject"] == "ana@example.org" and line["surface"] == "slack"
    assert line["chat_id"] == "chat-7" and line["order_id"] == 42
    assert AWS_KEY not in line["message"] and line["message"].startswith("key ")


def test_a_workflow_run_and_an_error_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    hub = tmp_path / "desk"
    hub.mkdir()
    ident = RunIdentity("workflow:nightly", None, "test", "workflow:nightly").to_dict()
    try:
        raise RuntimeError(f"failed with token {AWS_KEY}")
    except RuntimeError:
        exc = sys.exc_info()
    with context.run_scope(hub="desk", workflow="nightly", hub_dir=hub, engine=db.operational_engine(hub),
                           identity=ident, run_id="run-9", work={}):
        line = json.loads(logs.JsonFormatter().format(record("run step", exc_info=exc)))
    assert line["workflow"] == "nightly" and line["run_id"] == "run-9"
    assert "RuntimeError" in line["error"] and AWS_KEY not in line["error"]


def test_uvicorn_access_and_audit_records():
    fmt = logs.JsonFormatter()
    access_line = json.loads(fmt.format(record('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET",
                                               "/api/x?token=secretvalue", "1.1", 200, name="uvicorn.access")))
    assert access_line["client"] == "1.2.3.4" and access_line["method"] == "GET" and access_line["status"] == 200
    assert "secretvalue" not in access_line["path"]
    audit = {"event": "grant.added", "actor": "ana@example.org"}
    assert json.loads(fmt.format(record("ignored", hz_audit=audit))) == audit
    assert json.loads(logs.AuditLineFormatter().format(record("ignored", hz_audit=audit))) == audit


def test_a_full_audit_queue_drops_lines_and_says_so(capsys, monkeypatch):
    monkeypatch.setattr(logs._DropWhenFull, "dropped", 0)
    handler = logs._DropWhenFull(queue.Queue(maxsize=1))
    handler.enqueue(record("first"))
    handler.enqueue(record("second"))
    assert logs._DropWhenFull.dropped == 1
    assert "1 audit line(s) dropped" in capsys.readouterr().err
    rec = record("kept")
    assert handler.prepare(rec) is rec
