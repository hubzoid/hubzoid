"""Tests for the OTel env-builder (claude-local per-spawn attribution).

Behind HUBZOID_OTEL: when an OTLP endpoint is configured, we inject OTEL_* vars
plus per-spawn hubzoid.user / hubzoid.hub / hubzoid.surface resource attributes
into the `claude` subprocess env. Proven feasible by the Phase-0 spike.
"""
from __future__ import annotations

from hubzoid import otel


def _parse_attrs(env: dict) -> dict:
    return dict(kv.split("=", 1) for kv in env["OTEL_RESOURCE_ATTRIBUTES"].split(","))


def test_claude_env_carries_otel_vars_and_user_hub_attributes():
    env = otel.claude_otel_env(
        endpoint="http://collector:4318",
        user="priya",
        hub="irs-hub",
        surface="owui",
    )
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    # Traces-only by default: token + cost ride the spans, and a trace backend
    # (Langfuse) drops metrics/logs, so we don't waste egress emitting them.
    assert env["OTEL_METRICS_EXPORTER"] == "none"
    assert env["OTEL_LOGS_EXPORTER"] == "none"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4318"

    attrs = _parse_attrs(env)
    assert attrs["hubzoid.user"] == "priya"
    assert attrs["hubzoid.hub"] == "irs-hub"
    assert attrs["hubzoid.surface"] == "owui"
    # user.id mirrors the OWUI user so trace backends' NATIVE user views
    # (Langfuse Users tab keys on user.id) attribute to the real person, not
    # the shared claude-login subscription account.
    assert attrs["user.id"] == "priya"


def test_claude_env_metrics_logs_off_by_default_but_operator_can_opt_in(monkeypatch):
    monkeypatch.delenv("OTEL_METRICS_EXPORTER", raising=False)
    monkeypatch.delenv("OTEL_LOGS_EXPORTER", raising=False)
    env = otel.claude_otel_env(endpoint="http://c:4318", user="u", hub="h", surface="owui")
    assert env["OTEL_METRICS_EXPORTER"] == "none"
    assert env["OTEL_LOGS_EXPORTER"] == "none"
    # An operator with a metrics/log backend opts in via the standard env vars.
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
    env = otel.claude_otel_env(endpoint="http://c:4318", user="u", hub="h", surface="owui")
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"
    assert env["OTEL_LOGS_EXPORTER"] == "otlp"


def test_claude_env_protocol_defaults_http_but_honors_operator_override(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    env = otel.claude_otel_env(endpoint="http://c:4318", user="u", hub="h", surface="owui")
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"

    # An operator running a gRPC-only collector sets the standard var in the
    # hub .env; our default must not clobber it.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    env = otel.claude_otel_env(endpoint="http://c:4317", user="u", hub="h", surface="owui")
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"


def test_claude_env_empty_when_no_endpoint():
    # Off by default: no endpoint configured -> emit nothing, no behavior change.
    assert otel.claude_otel_env(endpoint=None, user="priya", hub="irs-hub", surface="owui") == {}
    assert otel.claude_otel_env(endpoint="", user="priya", hub="irs-hub", surface="owui") == {}


def test_attribute_values_are_sanitized_to_preserve_format():
    # A comma or equals in an identity must not corrupt the k=v,k=v format.
    env = otel.claude_otel_env(
        endpoint="http://c:4318", user="a,b=c", hub="irs-hub", surface="owui"
    )
    pairs = env["OTEL_RESOURCE_ATTRIBUTES"].split(",")
    assert len(pairs) == 4  # the comma in the user value did not add an extra pair
    attrs = _parse_attrs(env)
    assert "," not in attrs["hubzoid.user"]
    assert "," not in attrs["user.id"]
    assert attrs["hubzoid.hub"] == "irs-hub"


# ---------------------------------------------------------------------------
# config seam: HUBZOID_OTEL_ENDPOINT -> Settings.otel_endpoint (off by default)
# ---------------------------------------------------------------------------
def test_settings_reads_otel_endpoint(tmp_path, monkeypatch):
    from hubzoid import settings as settingslib

    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", "http://collector:4318")
    assert settingslib.load(tmp_path).otel_endpoint == "http://collector:4318"


def test_settings_otel_endpoint_defaults_none(tmp_path, monkeypatch):
    from hubzoid import settings as settingslib

    monkeypatch.delenv("HUBZOID_OTEL_ENDPOINT", raising=False)
    assert settingslib.load(tmp_path).otel_endpoint is None


# ---------------------------------------------------------------------------
# claude hot-path: per-turn options carry the current identity as attributes
# ---------------------------------------------------------------------------
def test_options_for_turn_injects_identity_when_otel_enabled():
    from claude_agent_sdk import ClaudeAgentOptions

    from hubzoid.access import identity as idmod
    from hubzoid.factory_claude import ClaudeRuntime

    rt = ClaudeRuntime(name="x", options=ClaudeAgentOptions(tools=[]),
                       hub="irs-hub", otel_endpoint="http://c:4318")
    with idmod.identity_scope(idmod.Identity.make(user="priya", surface="owui")):
        opts = rt._options_for_turn()

    env = opts.env or {}
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    attrs = dict(kv.split("=", 1) for kv in env["OTEL_RESOURCE_ATTRIBUTES"].split(","))
    assert attrs["hubzoid.user"] == "priya"
    assert attrs["hubzoid.hub"] == "irs-hub"


def test_options_for_turn_is_noop_when_otel_disabled():
    from claude_agent_sdk import ClaudeAgentOptions

    from hubzoid.factory_claude import ClaudeRuntime

    base = ClaudeAgentOptions(tools=[])
    rt = ClaudeRuntime(name="x", options=base, hub="irs-hub", otel_endpoint=None)
    assert rt._options_for_turn() is base  # untouched shared options


# ---------------------------------------------------------------------------
# openai path: litellm otel callback registered only when enabled
# ---------------------------------------------------------------------------
def test_openai_otel_setup_noop_when_disabled():
    from hubzoid import otel

    assert otel.openai_otel_setup(endpoint=None, hub="irs-hub") is False


def test_openai_otel_setup_registers_litellm_callback(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("litellm")
    fake.callbacks = []
    monkeypatch.setitem(sys.modules, "litellm", fake)
    for key in ("OTEL_EXPORTER", "OTEL_ENDPOINT", "OTEL_RESOURCE_ATTRIBUTES"):
        monkeypatch.setenv(key, "")  # contain env mutations to this test

    from hubzoid import otel

    assert otel.openai_otel_setup(endpoint="http://c:4318", hub="irs-hub") is True
    assert "otel" in fake.callbacks
