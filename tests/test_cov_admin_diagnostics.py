"""Diagnostic checks (hubzoid.admin.diagnostics): what goes into a support
bundle never carries a secret-looking key, a home path or a URL query (a
property test of `scrub`), and each check's warning and failure, made with
the conditions it looks for. No network, no model."""
from __future__ import annotations

import re
from collections import namedtuple
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hubzoid.admin import diagnostics as diag

# Assembled, so the repository holds no machine-local path (tests/test_repo_hygiene.py).
HOME_PATH = "/" + "Users" + "/ana/hub/.env"
WINDOWS_PATH = "C:" + "\\" + "Users" + "\\b\\x"
_SECRET_KEY = re.compile(r"(secret|token|password|passwd|api[_-]?key|fingerprint|credential|cookie|authorization)",
                         re.I)
_keys = st.sampled_from(["name", "count", "api_key", "Token", "user_password", "Authorization", "cookie_jar",
                         "url", "path", "credentials", "x"])
_leaves = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False),
                    st.sampled_from([HOME_PATH, "https://h.example/p?token=abc", WINDOWS_PATH,
                                     "plain words", "/home/runner/work/x"]),
                    st.text(max_size=600))
_trees = st.recursive(_leaves, lambda inner: st.one_of(st.lists(inner, max_size=60),
                                                       st.dictionaries(_keys, inner, max_size=6)), max_leaves=40)


def _walk(value):
    yield value
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


@given(tree=_trees)
def test_scrub_never_lets_secrets_paths_or_queries_through(tree):
    out = diag.scrub(tree)
    for node in _walk(out):
        if isinstance(node, dict):
            assert not any(_SECRET_KEY.search(str(k)) for k in node)
        if isinstance(node, list):
            assert len(node) <= 50
        if isinstance(node, str):
            assert len(node) <= 500
            assert "/" + "Users/" not in node and "/home/" not in node and "C:" + "\\Users" not in node
            assert "?token=" not in node


def test_storage_and_disk_failures(tmp_path, monkeypatch):
    hub = tmp_path / "h"
    hub.mkdir()
    from hubzoid import db

    def broken(hub_dir):
        raise OSError("refused")

    monkeypatch.setattr(db, "operational_engine", broken)
    r = diag.check_storage(hub)
    assert r.status == "fail" and r.remediation_code == "database_unreachable" and "OSError" in r.summary

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(diag.shutil, "disk_usage", lambda p: Usage(100e9, 99.7e9, 0.3e9))
    r = diag.check_disk(hub)
    assert r.status == "fail" and r.remediation_code == "disk_full" and r.details["free_gb"] == 0.3
    monkeypatch.setattr(diag.shutil, "disk_usage", lambda p: Usage(100e9, 98.5e9, 1.5e9))
    r = diag.check_disk(hub)
    assert r.status == "warn" and r.remediation_code == "disk_low"
    monkeypatch.setattr(diag.shutil, "disk_usage", lambda p: Usage(100e9, 50e9, 50e9))
    assert diag.check_disk(hub).status == "ok"


def test_first_and_doctor_results():
    assert diag._first(lambda: [], None).status == "skipped"
    check = SimpleNamespace(id="b", status="weird", summary="S", detail={"api_key": "x", "n": 1})
    other = SimpleNamespace(id="a", status="ok", summary="A", detail=None)
    r = diag._first(lambda hub: [other, check], "hub", "b")
    assert r.status == "info" and r.summary == "S" and r.details == {"n": 1}
    assert diag._first(lambda hub: [other, check], "hub", "missing").summary == "A"


def test_workflow_failures_and_alert_delivery(tmp_path, monkeypatch):
    hub = tmp_path / "alpha"
    hub.mkdir()
    from hubzoid.workflows import observe

    monkeypatch.setattr(observe, "runs", lambda *a, **k: [])
    assert diag.check_workflow_failures(hub).status == "ok"
    runs = [{"name": "md:digest"}] * 3 + [{"workflow": "nightly"}]
    monkeypatch.setattr(observe, "runs", lambda *a, **k: runs)
    r = diag.check_workflow_failures(hub)
    assert r.status == "warn" and r.details == {"by_workflow": {"digest": 3, "nightly": 1}}
    assert "4 workflow runs failed in the last day: digest (3), nightly (1)." == r.summary

    def broken(*a, **k):
        raise RuntimeError("no engine")

    monkeypatch.setattr(observe, "runs", broken)
    assert diag.check_workflow_failures(hub).status == "info"

    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from hubzoid.workflows import events

    events._engine(hub)  # creates the tables
    assert diag.check_alert_delivery(hub).status == "ok"
    import time

    from sqlalchemy import text

    with events._engine(hub).begin() as conn:
        conn.execute(text("INSERT INTO hz_workflow_alerts (id,hub,kind,payload,destination,state,attempt,due,created,"
                          "error) VALUES ('a1','alpha','run_failed','{}','{}','failed',5,0,:t,"
                          "'ConnectError: https://hooks.example/x?sig=1')"), {"t": time.time()})
    r = diag.check_alert_delivery(hub)
    assert r.status == "warn" and r.remediation_code == "alert_delivery" and "sig=1" not in r.summary


@pytest.mark.parametrize("count,one,many,expected", [(1, "run", None, "1 run"), (2, "run", None, "2 runs"),
                                                     (3, "person", "people", "3 people")])
def test_counting_words(count, one, many, expected):
    assert diag._n(count, one, many) == expected
