"""Unit tests for the workflow façade: durable state + the per-run hub proxy.

Pure: SQLite + contextvars, no DBOS launch, no model, no network.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from hubzoid.access import store_for
from hubzoid.workflows import WorkflowState, hub, run_scope
from hubzoid.workflows import context as wctx


@pytest.fixture(autouse=True)
def _reset_seams():
    # The call_llm/call_agent seams are module globals (set by server/cli boot or
    # runtime.launch); reset them around every façade test so a prior test that
    # launched DBOS can't leak a step-wrapped seam into these hermetic tests.
    wctx._LLM = wctx._AGENT = wctx._LLM_STEP = wctx._AGENT_STEP = None
    yield
    wctx._LLM = wctx._AGENT = wctx._LLM_STEP = wctx._AGENT_STEP = None


@pytest.fixture()
def engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'hub.db'}")


def test_state_roundtrip_and_isolation(engine):
    a = WorkflowState(engine, "finance", "wf_a")
    b = WorkflowState(engine, "finance", "wf_b")
    a["cursor"] = {"sha": "abc"}
    assert a["cursor"] == {"sha": "abc"}
    assert a.get("cursor") == {"sha": "abc"}
    assert "cursor" in a
    # same key, different workflow -> no clash
    assert b.get("cursor") is None
    assert "cursor" not in b
    del a["cursor"]
    assert a.get("cursor") is None


def test_state_survives_new_instance(engine):
    WorkflowState(engine, "finance", "wf")["k"] = 7
    # a fresh instance (a "restart") reads the persisted value
    assert WorkflowState(engine, "finance", "wf")["k"] == 7


def test_hub_requires_a_run():
    with pytest.raises(RuntimeError):
        _ = hub.name


def test_hub_proxy_in_scope(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    with run_scope(hub="finance", workflow="review_prs", hub_dir=tmp_path,
                   engine=engine, settings={"repo": "acme/app"}):
        assert hub.name == "finance"
        assert hub.setting("repo") == "acme/app"
        assert hub.setting("missing", "d") == "d"
        assert hub.secret("github_token") == "ghp_secret"   # from env, upper-cased
        hub.state["done"] = True
        assert hub.state["done"] is True
        assert hub.user.id == "workflow:review_prs"
    # context is cleared after the scope
    with pytest.raises(RuntimeError):
        _ = hub.name


def test_hub_scopes_nest_and_restore(engine, tmp_path):
    with run_scope(hub="a", workflow="w1", hub_dir=tmp_path, engine=engine):
        assert hub.name == "a"
        with run_scope(hub="b", workflow="w2", hub_dir=tmp_path, engine=engine):
            assert hub.name == "b"
        assert hub.name == "a"


def test_hub_user_can_consults_access_store(engine, tmp_path, monkeypatch):
    # point db.engine_for at our test engine so store_for/hub.user.can share it
    import hubzoid.db as db
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: engine)
    gs = store_for(tmp_path)
    gs.grant("workflow:review_prs", "finance", "prod_in")
    with run_scope(hub="finance", workflow="review_prs", hub_dir=tmp_path, engine=engine):
        assert hub.user.can("prod_in") is True
        assert hub.user.can("use_hub") is True       # implied
        assert hub.user.can("other_perm") is False


def test_call_llm_seam(engine, tmp_path):
    calls = []
    wctx.configure(llm=lambda prompt, **kw: calls.append((prompt, kw)) or "REVIEW")
    try:
        with run_scope(hub="finance", workflow="w", hub_dir=tmp_path, engine=engine):
            assert hub.call_llm("hello") == "REVIEW"
        assert calls[0][0] == "hello"
        assert calls[0][1]["hub_dir"] == tmp_path
    finally:
        wctx._LLM = None


def test_call_llm_unconfigured_raises(engine, tmp_path):
    wctx._LLM = None
    with run_scope(hub="finance", workflow="w", hub_dir=tmp_path, engine=engine):
        with pytest.raises(RuntimeError):
            hub.call_llm("hi")
