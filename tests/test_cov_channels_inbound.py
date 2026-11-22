"""Inbound configuration, bridge streaming, media failures and history degradation."""
from __future__ import annotations

import json
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from hubzoid.inbound import dispatch, history, routing, run
from hubzoid.inbound.message import MediaRef, ext_for
from tests.test_slack_adapter import _FakeSSEResponse, _sse_lines


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "WEBUI_AUTH", "HUBZOID_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "desk"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: A Nice Desk\n---\nHelpful.")
    return root


@pytest.mark.parametrize("overrides,expected", [({}, (40, None, 1.0)),
    ({"INBOUND_HISTORY_MAX": "bad", "INBOUND_HISTORY_TTL_DAYS": "bad", "INBOUND_STREAM_INTERVAL": "bad"}, (40, None, 1.0)),
    ({"INBOUND_HISTORY_MAX": "8", "INBOUND_HISTORY_TTL_DAYS": "2", "INBOUND_STREAM_INTERVAL": ".1"}, (8, 172800, .8))])
def test_inbound_builder_uses_stream_defaults_and_bounds(hub_dir, monkeypatch, overrides, expected):
    build = Mock(return_value="app")
    monkeypatch.setattr(run, "build_app", build)
    env = {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_WEBHOOK_SECRET": "secret", **overrides}
    assert run.build_app_for_hub(hub_dir, env) == "app"
    options = build.call_args.kwargs
    assert (options["history_max"], options["history_ttl_seconds"], options["stream_interval"]) == expected
    assert options["telegram"].stream and options["whatsapp"] is None
    assert options["model"] == "a-nice-desk"
    assert options["bridge_url"].startswith("http://127.0.0.1:")


def test_inbound_surface_filter_can_disable_configured_surface(hub_dir, monkeypatch):
    env = {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_WEBHOOK_SECRET": "secret", "HUBZOID_INBOUND_SURFACES": "whatsapp"}
    build = Mock()
    monkeypatch.setattr(run, "build_app", build)
    assert run.build_app_for_hub(hub_dir, env) is None
    assert build.call_count == 0
    assert run.run(hub_dir, {}) == 1


def test_inbound_server_entry_uses_operator_host_and_port_without_starting_server(hub_dir, monkeypatch):
    import uvicorn
    app = object()
    monkeypatch.setattr(run, "build_app_for_hub", lambda *a: app)
    serve = Mock()
    monkeypatch.setattr(uvicorn, "run", serve)
    assert run.run(hub_dir, {"HUBZOID_INBOUND_HOST": " 0.0.0.0 ", "HUBZOID_HUB_SLUG": "desk"}, port=8123) == 0
    serve.assert_called_once_with(app, host="0.0.0.0", port=8123, log_level="info")


def test_model_fallback_and_slug_normalization(hub_dir, monkeypatch):
    from hubzoid.loaders import agents
    monkeypatch.setattr(agents, "load_main", Mock(side_effect=ValueError("bad file")))
    assert run._model_label(hub_dir) == "desk"
    assert run._slugify(" A -- Nice!! Desk ") == "a-nice-desk"
    assert run._slugify("") == "agent"
    assert routing._slugify("!!") == "hub"
    assert routing.inbound_port({"HUBZOID_INBOUND_PORT": "invalid"}) == routing.DEFAULT_INBOUND_PORT
    monkeypatch.setenv("HUBZOID_INBOUND_PORT", "8999")
    assert routing.inbound_port() == 8999
    assert routing.hub_slug(hub_dir, {"HUBZOID_HUB_SLUG": " A -- Hub "}) == "a-hub"
    assert ext_for(None) == "" and ext_for("application/x-unknown") == ""


def test_connect_journey_disables_when_store_is_unwritable(hub_dir, monkeypatch, caplog):
    from hubzoid import db
    monkeypatch.setattr(db, "shared_store_file", lambda *a: hub_dir / "shared.db")
    monkeypatch.setattr(run, "_writable", lambda _: False)
    assert run._connect_journeys(hub_dir, {}, whatsapp=True) is False
    assert "ReadWritePaths" in caplog.text
    monkeypatch.setattr(db, "shared_store_file", Mock(side_effect=ValueError("bad database")))
    assert run._connect_journeys(hub_dir, {}, whatsapp=True) is False
    assert "bad database" in caplog.text
    monkeypatch.setattr(db, "shared_store_file", lambda *a: None)
    assert run._connect_journeys(hub_dir, {}, whatsapp=True) is True


def test_writable_requires_parent_store_and_migration_lock(hub_dir, monkeypatch):
    path = hub_dir / "shared.db"
    path.touch()
    lock = path.with_name(path.name + ".operational.migrate.lock")
    lock.touch()
    monkeypatch.setattr(run.os, "access", lambda p, mode: p != lock)
    assert not run._writable(path)
    monkeypatch.setattr(run.os, "access", lambda p, mode: True)
    assert run._writable(path)


@pytest.mark.parametrize("line", [b"\xff", "data: {", "data: {}", "data: []", "data: {\"choices\":[]}",
    "data: {\"choices\":[null]}", "data: {\"choices\":[{\"delta\":{\"content\":1}}]}", "event: message"])
def test_sse_parser_ignores_invalid_payloads(line):
    assert dispatch.parse_sse_delta(line) is None


def test_dispatch_callback_failure_does_not_drop_reply_and_owned_client_closes(monkeypatch):
    client = Mock()
    client.stream.return_value = _FakeSSEResponse(_sse_lines("hello", " world"))
    monkeypatch.setattr(httpx, "Client", Mock(return_value=client))
    delta = Mock(side_effect=OSError("UI unavailable"))
    assert dispatch.dispatch(bridge_url="http://bridge/v1", api_key="key", model="desk", messages=[], surface="telegram",
                             on_delta=delta, timeout=7) == "hello world"
    assert delta.call_count == 2 and client.close.call_count == 1


@pytest.mark.parametrize("provider", ["telegram", "whatsapp"])
@pytest.mark.parametrize("failure", ["no_url", "bad_download", "empty_download", "exception"])
def test_media_fetch_failure_is_best_effort_and_closes_owned_client(monkeypatch, provider, failure):
    module = import_module(f"hubzoid.{provider}.media")
    client = Mock()
    if provider == "telegram":
        first = {"ok": True, "result": {} if failure == "no_url" else {"file_path": "docs/file"}}
    else:
        first = {} if failure == "no_url" else {"url": "https://media.example.test/file"}
    client.get.side_effect = (OSError("offline") if failure == "exception" else [httpx.Response(200, json=first),
        httpx.Response(403 if failure == "bad_download" else 200, content=b"" if failure == "empty_download" else b"file")])
    monkeypatch.setattr(httpx, "Client", lambda **kw: client)
    assert module.fetch(MediaRef("file1", "report.pdf"), token="fake-token") is None
    assert client.close.call_count == 1


@pytest.mark.parametrize("name,kwargs,endpoint", [
    ("send_message", {"text": "Hello", "reply_markup": {"keyboard": [["Share"]]}}, "sendMessage"),
    ("edit_message_text", {"message_id": 7, "text": "Updated"}, "editMessageText"),
    ("send_chat_action", {}, "sendChatAction")])
def test_telegram_send_owns_client_and_sends_exact_operation(monkeypatch, name, kwargs, endpoint):
    module = import_module("hubzoid.telegram.send")
    captured = []
    def handler(req):
        captured.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    real_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client)
    assert getattr(module, name)(token="fake", chat_id="42", **kwargs)["ok"]
    assert real_client.is_closed
    assert captured[0][0].endswith("/botfake/" + endpoint)
    assert captured[0][1]["chat_id"] == "42"
    if name == "send_message":
        assert captured[0][1]["reply_markup"] == {"keyboard": [["Share"]]}


def test_history_parts_decode_and_database_failure_are_best_effort(hub_dir, monkeypatch, caplog):
    from hubzoid.db import engine_for
    hist = history.History(engine_for(hub_dir))
    parts = [{"type": "text", "text": "Hello"}, {"type": "file", "file_id": "file1"}]
    hist.append("chat", "user", parts)
    assert hist.load("chat") == [{"role": "user", "content": parts}]
    assert history._decode(history._PARTS + "{") == ""
    monkeypatch.setattr(hist.engine, "connect", Mock(side_effect=OSError("read failed")))
    assert hist.load("chat") == [] and "load failed" in caplog.text
    monkeypatch.setattr(hist.engine, "begin", Mock(side_effect=OSError("write failed")))
    hist.append("chat", "user", "lost")
    assert "append failed" in caplog.text
