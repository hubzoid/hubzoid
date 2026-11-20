"""Langfuse v4 ingestion header on every OTLP export path.

Langfuse Cloud v4 asks OTLP senders for `x-langfuse-ingestion-version: 4`.
Hubzoid adds it by itself when the endpoint is Langfuse (path ends in
`/api/public/otel`), so no deployment has to put it in `.env`. An operator's
own value wins and is never duplicated. Other OTLP endpoints are untouched.
"""
from __future__ import annotations

import sys
import types

import pytest

from hubzoid import otel

LF = "https://us.cloud.langfuse.com/api/public/otel"
H = "x-langfuse-ingestion-version"


@pytest.mark.parametrize("endpoint", [
    LF,
    LF + "/",
    "https://cloud.langfuse.com/api/public/otel",
    "http://langfuse.internal:3000/api/public/otel",           # self-hosted
    "https://lf.example/prefix/api/public/otel",                # behind a path prefix
    LF + "/v1/traces",                                          # full traces URL
    "  https://US.CLOUD.LANGFUSE.COM/API/PUBLIC/OTEL  ",
])
def test_langfuse_endpoints_detected(endpoint):
    assert otel.is_langfuse_endpoint(endpoint)


@pytest.mark.parametrize("endpoint", [
    None, "",
    "http://collector:4318",
    "http://127.0.0.1:8000/otel",                               # the bridge's own relay
    "https://otlp.example.com/v1/traces",
    "https://api.honeycomb.io",
    "https://lf.example/api/public/otel-proxy",
    "https://lf.example/api/public/ingestion",                  # legacy, not OTLP
    "https://evil.example/?next=/api/public/otel",              # query, not path
])
def test_other_endpoints_not_detected(endpoint):
    assert not otel.is_langfuse_endpoint(endpoint)


# --- dict form (the bridge relay) ---------------------------------------------
def test_dict_adds_header_for_langfuse_and_keeps_auth():
    out = otel.with_langfuse_headers({"Authorization": "Basic abc="}, LF)
    assert out == {"Authorization": "Basic abc=", H: "4"}


def test_dict_operator_value_wins_any_case():
    given = {"X-Langfuse-Ingestion-Version": "5"}
    out = otel.with_langfuse_headers(given, LF)
    assert out == given  # not overwritten, not duplicated under another case


def test_dict_non_langfuse_untouched_and_input_not_mutated():
    given = {"Authorization": "Bearer t"}
    assert otel.with_langfuse_headers(given, "http://collector:4318") == given
    otel.with_langfuse_headers(given, LF)
    assert given == {"Authorization": "Bearer t"}
    assert otel.with_langfuse_headers(None, "http://collector:4318") == {}


# --- string form (OTEL_EXPORTER_OTLP_HEADERS) -----------------------------------
def test_string_appends_after_basic_auth_padding():
    raw = "Authorization=Basic cGs6c2s="
    out = otel.langfuse_otlp_headers(raw, LF)
    assert out == "Authorization=Basic cGs6c2s=,x-langfuse-ingestion-version=4"
    assert otel.parse_otlp_headers(out) == {"Authorization": "Basic cGs6c2s=", H: "4"}


def test_string_empty_and_trailing_comma():
    assert otel.langfuse_otlp_headers(None, LF) == f"{H}=4"
    assert otel.langfuse_otlp_headers("", LF) == f"{H}=4"
    assert otel.langfuse_otlp_headers("a=b,", LF) == f"a=b,{H}=4"


def test_string_operator_value_wins_and_is_idempotent():
    raw = "Authorization=Basic x,X-LANGFUSE-INGESTION-VERSION=4"
    assert otel.langfuse_otlp_headers(raw, LF) == raw
    once = otel.langfuse_otlp_headers("Authorization=Basic x", LF)
    assert otel.langfuse_otlp_headers(once, LF) == once


def test_string_non_langfuse_unchanged():
    raw = "Authorization=Bearer t"
    assert otel.langfuse_otlp_headers(raw, "http://collector:4318") == raw
    assert otel.langfuse_otlp_headers(None, "http://collector:4318") == ""


# --- claude subprocess (exports directly when normalize is off) ---------------
def test_claude_env_adds_header_when_exporting_straight_to_langfuse(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc=")
    env = otel.claude_otel_env(endpoint=LF, user="u", hub="h", surface="s")
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization=Basic abc=,{H}=4"


def test_claude_env_leaves_headers_alone_when_already_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization=Basic abc=,{H}=4")
    env = otel.claude_otel_env(endpoint=LF, user="u", hub="h", surface="s")
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env  # inherited unchanged


@pytest.mark.parametrize("endpoint", ["http://collector:4318", "http://127.0.0.1:8000/otel"])
def test_claude_env_no_header_for_collector_or_relay(monkeypatch, endpoint):
    # Through the relay (loopback) the bridge adds the header on forward.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc=")
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", LF)
    env = otel.claude_otel_env(endpoint=endpoint, user="u", hub="h", surface="s")
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env


# --- OpenAI/LiteLLM path (reads OTEL_EXPORTER_OTLP_HEADERS from the process) ---
def _fake_litellm(monkeypatch):
    fake = types.ModuleType("litellm")
    fake.callbacks = []
    monkeypatch.setitem(sys.modules, "litellm", fake)
    for key in ("OTEL_EXPORTER", "OTEL_ENDPOINT", "OTEL_RESOURCE_ATTRIBUTES"):
        monkeypatch.setenv(key, "")


def test_litellm_path_gets_header_for_langfuse(monkeypatch):
    import os
    _fake_litellm(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc=")
    assert otel.openai_otel_setup(endpoint=LF, hub="h") is True
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization=Basic abc=,{H}=4"
    otel.openai_otel_setup(endpoint=LF, hub="h")  # a second hub load adds nothing
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"].count(H) == 1


def test_litellm_path_untouched_for_collector(monkeypatch):
    import os
    _fake_litellm(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer t")
    otel.openai_otel_setup(endpoint="http://collector:4318", hub="h")
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer t"


# --- bridge relay route forwards with the header -------------------------------
def test_bridge_relay_forwards_with_ingestion_header(monkeypatch):
    from tests.test_otel_normalize import _client, _request

    captured = {}

    async def fake_forward(body, *, forward_url, headers, client, gzipped=False):
        captured["headers"] = headers
        return 200

    monkeypatch.setattr("hubzoid.otel.forward_otlp", fake_forward)
    client = _client(monkeypatch)
    r = client.post("/otel/v1/traces", content=_request(span_attrs={"input_tokens": 1}),
                    headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 200
    assert captured["headers"] == {"Authorization": "Basic abc123", H: "4"}


# --- review fixes: follow where each exporter really sends and reads -----------
def _clear_litellm_env(monkeypatch):
    for key in ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_HEADERS",
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(key, raising=False)


def test_litellm_keeps_auth_in_otel_headers(monkeypatch):
    import os
    _fake_litellm(monkeypatch)
    _clear_litellm_env(monkeypatch)
    monkeypatch.setenv("OTEL_HEADERS", "Authorization=Basic abc=")
    otel.openai_otel_setup(endpoint=LF, hub="h")
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in os.environ  # would hide OTEL_HEADERS
    assert os.environ["OTEL_HEADERS"] == f"Authorization=Basic abc=,{H}=4"


def test_litellm_keeps_auth_in_traces_headers(monkeypatch):
    import os
    _fake_litellm(monkeypatch)
    _clear_litellm_env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=Basic abc=")
    otel.openai_otel_setup(endpoint=LF, hub="h")
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in os.environ
    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == f"Authorization=Basic abc=,{H}=4"


def test_litellm_creates_generic_headers_when_none_set(monkeypatch):
    import os
    _fake_litellm(monkeypatch)
    _clear_litellm_env(monkeypatch)
    otel.openai_otel_setup(endpoint=LF, hub="h")
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == f"{H}=4"


def test_litellm_follows_its_preferred_endpoint(monkeypatch):
    import os
    _fake_litellm(monkeypatch)
    _clear_litellm_env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer t")
    # LiteLLM sends to OTEL_EXPORTER_OTLP_ENDPOINT over OTEL_ENDPOINT.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    otel.openai_otel_setup(endpoint=LF, hub="h")
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == "Authorization=Bearer t"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", LF)
    otel.openai_otel_setup(endpoint="http://collector:4318", hub="h")
    assert os.environ["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization=Bearer t,{H}=4"


def test_claude_env_follows_operator_traces_endpoint(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc=")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", LF + "/v1/traces")
    env = otel.claude_otel_env(endpoint="http://collector:4318", user="u", hub="h", surface="s")
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization=Basic abc=,{H}=4"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://collector:4318/v1/traces")
    env = otel.claude_otel_env(endpoint=LF, user="u", hub="h", surface="s")
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env


# --- second review: empty LiteLLM vars, Claude trace-specific operator value ----
@pytest.mark.parametrize("empty_var", ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_HEADERS"])
def test_litellm_empty_var_keeps_traces_headers_auth(monkeypatch, empty_var):
    import os
    _fake_litellm(monkeypatch)
    _clear_litellm_env(monkeypatch)
    monkeypatch.setenv(empty_var, "")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=Basic abc=")
    otel.openai_otel_setup(endpoint=LF, hub="h")
    assert os.environ[empty_var] == ""  # still empty, so the exporter falls back
    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == f"Authorization=Basic abc=,{H}=4"


def test_claude_env_respects_operator_version_in_traces_headers(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc=")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", f"{H}=5")
    env = otel.claude_otel_env(endpoint=LF, user="u", hub="h", surface="s")
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
