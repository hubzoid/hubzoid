"""The hub side of an AgentCore call (hubzoid.runner.client._Transport): the
region, the event stream, which AWS errors are retried (only those that prove
the request never reached the runner) and how each maps to a RunnerError, the
HTTP endpoint used in development, and stopping a session. No AWS: the boto3
client and the HTTP transport are fakes."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from hubzoid.runner import client as rc
from hubzoid.runner import protocol

ARN = "arn:aws:bedrock-agentcore:eu-west-2:123456789012:runtime/hz-abc"


def settings(**kw):
    base = {"agentcore_runtime_arn": ARN, "agentcore_qualifier": None, "agentcore_endpoint": None,
            "agentcore_region": None}
    return SimpleNamespace(**{**base, **kw})


class _Raw:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read1(self, n):
        return self.chunks.pop(0) if self.chunks else b""


class _Boto:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls, self.stopped = [], []

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return {"response": SimpleNamespace(_raw_stream=_Raw(out))}

    def stop_runtime_session(self, **kw):
        self.stopped.append(kw)
        if kw.get("runtimeSessionId") == "boom":
            raise RuntimeError("network")


def aws_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeAgentRuntime")


def transport_with(fake, **kw):
    t = rc._Transport(settings(**kw))
    t._local.client = fake
    return t


def stream(*events):
    return [protocol.sse(e) for e in events]


def test_region_comes_from_settings_then_the_arn():
    assert rc._Transport(settings(agentcore_region="us-east-1"))._region() == "us-east-1"
    assert rc._Transport(settings())._region() == "eu-west-2"
    assert rc._Transport(settings(agentcore_runtime_arn="bad"))._region() is None
    assert rc._Transport(settings()).qualifier == "DEFAULT"
    assert rc.transport(settings()) is rc.transport(settings())  # one per configuration


def test_events_stream_back_from_aws():
    fake = _Boto([stream({"type": "start"}, {"type": "end", "outputs": {}})])
    t = transport_with(fake)
    got = list(t.invoke("sess-1", {"call": "rc_1"}, threading.Event()))
    assert [e["type"] for e in got] == ["start", "end"]
    sent = fake.calls[0]
    assert sent["agentRuntimeArn"] == ARN and sent["runtimeSessionId"] == "sess-1"
    assert sent["accept"] == "text/event-stream" and b'"rc_1"' in sent["payload"]


def test_only_errors_before_the_runner_are_retried(monkeypatch):
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)
    fake = _Boto([aws_error("ThrottlingException"), aws_error("RetryableConflictException"),
                  stream({"type": "end", "outputs": {}})])
    got = list(transport_with(fake).invoke("s", {}, threading.Event()))
    assert len(fake.calls) == 3 and got[-1]["type"] == "end"

    for code, mapped in (("AccessDeniedException", "runner_unavailable"), ("ValidationException", "runner_config"),
                         ("ServiceQuotaExceededException", "runner_quota"), ("RuntimeClientError", "runner_failed"),
                         ("SomethingNew", "runner_unavailable")):
        fake = _Boto([aws_error(code)])
        with pytest.raises(rc.RunnerError) as err:
            list(transport_with(fake).invoke("s", {}, threading.Event()))
        assert err.value.code == mapped and err.value.detail == code and len(fake.calls) == 1

    fake = _Boto([EndpointConnectionError(endpoint_url="https://x")])
    with pytest.raises(rc.RunnerError) as err:
        list(transport_with(fake).invoke("s", {}, threading.Event()))
    assert err.value.code == "runner_unavailable" and err.value.detail == "EndpointConnectionError"


def test_no_runtime_arn_is_a_configuration_error():
    with pytest.raises(rc.RunnerError) as err:
        list(rc._Transport(settings(agentcore_runtime_arn=None)).invoke("s", {}, threading.Event()))
    assert err.value.code == "runner_config" and "HUBZOID_AGENTCORE_RUNTIME_ARN" in str(err.value)


def test_reading_stops_when_asked_and_without_read1():
    stop = threading.Event()
    chunks = iter([b"a", b"b"])
    raw = SimpleNamespace(read=lambda n: next(chunks, b""))
    assert list(rc._read_available(raw, stop)) == [b"a", b"b"]
    stop.set()
    assert list(rc._read_available(_Raw([b"never"]), stop)) == []
    assert list(rc._until_stopped([b"x", b"y"], stop)) == []
    assert list(rc._until_stopped([b"x", b"y"], threading.Event())) == [b"x", b"y"]


def test_stop_session_is_best_effort_and_skipped_for_http():
    fake = _Boto([])
    t = transport_with(fake)
    t.stop_session("sess-9")
    t.stop_session("boom")                      # an error is logged, not raised
    assert [c["runtimeSessionId"] for c in fake.stopped] == ["sess-9", "boom"]
    http = transport_with(_Boto([]), agentcore_endpoint="http://127.0.0.1:9")
    http.stop_session("sess-9")
    assert http._local.client.stopped == []


_REAL_CLIENT = httpx.Client


def _http(monkeypatch, handler):
    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*a, **kw)

    monkeypatch.setattr(httpx, "Client", factory)


def test_the_development_endpoint_streams_and_reports_failures(monkeypatch):
    t = rc._Transport(settings(agentcore_endpoint="http://runner.test/"))
    assert t.endpoint == "http://runner.test"

    def ok(request):
        assert request.url.path == "/invocations"
        assert request.headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] == "sess-2"
        body = b"".join(stream({"type": "start"}, {"type": "end", "outputs": {}}))
        return httpx.Response(200, stream=httpx.ByteStream(body))

    _http(monkeypatch, ok)
    assert [e["type"] for e in t.invoke("sess-2", {}, threading.Event())] == ["start", "end"]

    _http(monkeypatch, lambda request: httpx.Response(502))
    with pytest.raises(rc.RunnerError) as err:
        list(t.invoke("sess-2", {}, threading.Event()))
    assert err.value.code == "runner_failed" and err.value.detail == "HTTP 502"

    def down(request):
        raise httpx.ConnectError("refused")

    _http(monkeypatch, down)
    with pytest.raises(rc.RunnerError) as err:
        list(t.invoke("sess-2", {}, threading.Event()))
    assert err.value.code == "runner_unavailable" and err.value.detail == "ConnectError"
