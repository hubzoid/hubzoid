"""Tests for the optional Langfuse push.

The load-bearing property is that Langfuse is an upgrade, never a dependency:
an unconfigured hub must reach no network at all, and a Langfuse outage must
never turn a green suite red.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hubzoid.evals import langfuse as lf
from hubzoid.evals.assertions import Check
from hubzoid.evals.results import CaseResult, JudgeResult, SuiteResult

ENDPOINT = "https://langfuse.internal/api/public/otel"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("HUBZOID_OTEL_ENDPOINT", "LANGFUSE_PUBLIC_KEY",
                "LANGFUSE_SECRET_KEY", "OTEL_EXPORTER_OTLP_HEADERS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def hub(tmp_path) -> Path:
    h = tmp_path / "myhub"
    h.mkdir()
    return h


def _suite() -> SuiteResult:
    suite = SuiteResult(hub="myhub", started_at="2026-08-20T10:00:00",
                        finished_at="2026-08-20T10:01:00", model="claude-local")
    suite.cases.append(CaseResult(
        name="refund", tags=["canary"], response="14 days",
        tool_calls=["read_knowledge"], duration=2.5,
        checks=[Check("contains", True), Check("expect_tools", True)],
        judge=JudgeResult(score=9, threshold=7, reasoning="cites policy"),
    ))
    suite.cases.append(CaseResult(
        name="leak", checks=[Check("forbid_tools", False, "forbidden tool called: http_get")]))
    return suite


# ---------------------------------------------------------------------------
# configuration discovery
# ---------------------------------------------------------------------------
def test_unconfigured_hub_is_not_configured(hub):
    assert lf.config(hub) is None


def test_endpoint_without_keys_is_not_configured(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", ENDPOINT)
    assert lf.config(hub) is None


def test_langfuse_env_keys_are_used(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-1")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-1")
    assert lf.config(hub) == ("https://langfuse.internal", "pk-lf-1", "sk-lf-1")


def test_keys_are_recovered_from_the_otel_basic_header(hub, monkeypatch):
    """docs/OBSERVABILITY.md tells operators to set Basic auth there. Reading
    it back means one place to configure, not two."""
    import base64
    token = base64.b64encode(b"pk-lf-2:sk-lf-2").decode()
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", f"Authorization=Basic {token}")
    assert lf.config(hub) == ("https://langfuse.internal", "pk-lf-2", "sk-lf-2")


def test_garbled_otel_header_is_ignored(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic !!!notbase64")
    assert lf.config(hub) is None


def test_malformed_endpoint_is_not_configured(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", "not-a-url")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    assert lf.config(hub) is None


def test_dataset_is_named_per_hub(hub):
    assert lf.dataset_name(hub) == "hubzoid-evals-myhub"


# ---------------------------------------------------------------------------
# the ingestion batch
# ---------------------------------------------------------------------------
def test_events_carry_a_trace_and_scores_per_case(hub):
    events = lf._events(hub, _suite(), "run1")
    kinds = [e["type"] for e in events]
    assert kinds.count("trace-create") == 2
    names = {e["body"].get("name") for e in events if e["type"] == "score-create"}
    assert {"passed", "contains", "expect_tools", "judge", "forbid_tools"} <= names


def test_every_trace_is_tagged_so_dashboards_can_filter_evals_out(hub):
    """Eval traffic is synthetic; leaving it mixed into production traces
    would quietly skew the latency and cost numbers operators rely on."""
    traces = [e for e in lf._events(hub, _suite(), "run1") if e["type"] == "trace-create"]
    assert traces
    for t in traces:
        assert lf.EVAL_TAG in t["body"]["tags"]
        assert t["body"]["metadata"][lf.EVAL_TAG] is True


def test_passed_score_reflects_the_verdict(hub):
    events = lf._events(hub, _suite(), "run1")
    passed = {e["body"]["traceId"]: e["body"]["value"]
              for e in events
              if e["type"] == "score-create" and e["body"]["name"] == "passed"}
    assert passed["eval-run1-refund"] == 1
    assert passed["eval-run1-leak"] == 0


def test_judge_score_is_the_raw_number_with_its_reasoning(hub):
    judge = next(e for e in lf._events(hub, _suite(), "run1")
                 if e["type"] == "score-create" and e["body"]["name"] == "judge")
    assert judge["body"]["value"] == 9
    assert judge["body"]["comment"] == "cites policy"


def test_a_judge_error_produces_no_judge_score(hub):
    """A broken grader must not be recorded as a score of zero — that would
    read as a hub regression on the trend chart."""
    suite = _suite()
    suite.cases[0].judge = JudgeResult(score=0, threshold=7, error="rate limited")
    names = [e["body"]["name"] for e in lf._events(hub, suite, "r")
             if e["type"] == "score-create"]
    assert "judge" not in names


def test_event_ids_are_unique(hub):
    events = lf._events(hub, _suite(), "run1")
    ids = [e["id"] for e in events]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------
def test_push_is_a_noop_and_makes_no_request_when_unconfigured(hub, monkeypatch):
    import httpx

    def boom(*a, **k):  # pragma: no cover — must not run
        raise AssertionError("network touched on an unconfigured hub")

    monkeypatch.setattr(httpx, "post", boom)
    assert lf.push(hub, _suite()) is None


def _configured(monkeypatch):
    monkeypatch.setenv("HUBZOID_OTEL_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")


def test_push_posts_the_batch_with_basic_auth(hub, monkeypatch):
    import httpx
    seen = {}

    class Resp:
        status_code = 207
        text = ""

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return Resp()

    _configured(monkeypatch)
    monkeypatch.setattr(httpx, "post", fake_post)

    status = lf.push(hub, _suite(), run_name="run1")
    assert "2 case(s)" in status
    assert seen["url"] == "https://langfuse.internal/api/public/ingestion"
    assert seen["headers"]["Authorization"].startswith("Basic ")
    assert len(seen["json"]["batch"]) > 4


def test_push_raises_on_an_error_response(hub, monkeypatch):
    """The caller catches and warns; the local JSON stays the record."""
    import httpx

    class Resp:
        status_code = 401
        text = "unauthorized"

    _configured(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="401"):
        lf.push(hub, _suite())


def test_a_langfuse_outage_never_fails_the_suite(hub, monkeypatch):
    import httpx

    _configured(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
        httpx.ConnectError("no route to host")))

    from hubzoid import cli
    cli._push_to_langfuse(hub, _suite())        # must not raise
