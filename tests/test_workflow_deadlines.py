"""Focused, provider-free coverage for workflow call deadlines."""
from __future__ import annotations

import asyncio
import time

import pytest

from hubzoid import runtime
from hubzoid.workflows import deadlines


def test_nested_deadline_caps_call_and_expires():
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        with deadlines.scope(0.05):
            assert 0 < deadlines.remaining(10) <= 0.05
            with deadlines.scope(10):
                assert 0 < deadlines.remaining(10) <= 0.05
            time.sleep(0.06)
            deadlines.remaining()
    assert deadlines.remaining() is None


def test_litellm_completion_is_cancelled_at_call_deadline(tmp_path, monkeypatch):
    import litellm

    hub_dir = tmp_path / "hub"
    hub_dir.mkdir()
    (hub_dir / "AGENTS.md").write_text("---\nname: hub\ndescription: d\n---\nbody")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    cancelled = []

    async def slow_completion(**kwargs):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(litellm, "acompletion", slow_completion)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        runtime.complete_once(hub_dir, {"prompt": "hello", "model": "openai/fake",
                                        "timeout": 0.05, "response_format": "text"})
    assert cancelled == [True]
    assert time.monotonic() - started < 2
