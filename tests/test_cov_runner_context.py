"""A turn's context carried from the hub service to an AgentCore runner
(hubzoid.runner.context): an identity survives the trip (a property test),
nobody stays anonymous, and what the service captured is what the runner's
turn sees, then the runner's outputs come back."""
from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from hubzoid import _request_ctx, access, usage
from hubzoid.access import identity as idlib
from hubzoid.runner import context as rc

_names = st.from_regex(r"[a-z]{1,8}@example\.org", fullmatch=True)


@given(user=_names, groups=st.sets(st.sampled_from(["ops", "finance", "sales"])),
       surface=st.sampled_from(["web", "slack", "api"]), hubs=st.one_of(st.none(), st.lists(st.sampled_from(["a", "b"]))),
       epoch=st.one_of(st.none(), st.integers(0, 9)))
def test_an_identity_survives_the_trip(user, groups, surface, hubs, epoch):
    ident = idlib.Identity.make(user, tuple(groups), surface=surface, account_id="acc-1", auth_epoch=epoch,
                                hubs=tuple(hubs) if hubs is not None else None)
    carried = json.loads(json.dumps(rc.identity_dict(ident)))           # it travels as JSON
    back = rc.identity_from(carried)
    assert (back.user, set(back.groups), back.surface, back.account_id, back.auth_epoch, back.hubs) == \
           (ident.user, set(ident.groups), ident.surface, ident.account_id, ident.auth_epoch, ident.hubs)


def test_nobody_is_anonymous_and_an_app_caller_keeps_its_scope():
    assert rc.identity_from(None) is idlib.ANONYMOUS and rc.identity_from({}) is idlib.ANONYMOUS
    app = idlib.Identity.make(None, (), surface="api",
                              app=idlib.AppScope("app-1", frozenset({"desk"}), frozenset({"lookup"})))
    back = rc.identity_from(json.loads(json.dumps(rc.identity_dict(app))))
    assert back.app.app_id == "app-1" and back.app.agents == frozenset({"desk"})
    assert back.app.tools == frozenset({"lookup"})


def test_what_the_service_captures_is_what_the_runner_sees():
    ident = idlib.Identity.make("ana@example.org", ("ops",), surface="slack", account_id="acc-9")
    slot = _request_ctx.SessionSlot(resume_id="sess-1", delta_prompt="next", images=None, delta_images=None,
                                    resume_cost=0.5)
    slot.model = "sonnet"
    tag = usage._RUN_TAG.set("run-tag-1")
    try:
        with access.identity_scope(ident), _request_ctx.chat_scope("chat-3"), \
                _request_ctx.native_session_scope(slot):
            captured = json.loads(json.dumps(rc.capture()))
    finally:
        usage._RUN_TAG.reset(tag)
    assert captured["chat"] == "chat-3" and captured["run_tag"] == "run-tag-1"
    assert captured["slot"]["resume_id"] == "sess-1" and captured["slot"]["model"] == "sonnet"

    guard = object()
    with rc.restore(captured, rc.identity_dict(ident), guard) as restored:
        assert _request_ctx.get_chat_id() == "chat-3" and usage._RUN_TAG.get() == "run-tag-1"
        assert access.current_identity().user == "ana@example.org"
        assert restored.resume_id == "sess-1" and restored.model == "sonnet" and restored.guard is guard
        restored.session_id, restored.runtime = "sess-2", "claude"
        _request_ctx.record_artifact("report.pdf", "http://h/artifacts/c/report.pdf?t=x")
        out = json.loads(json.dumps(rc.outputs(restored)))
    assert _request_ctx.get_chat_id() is None and usage._RUN_TAG.get() != "run-tag-1"   # scopes are left
    assert out["slot"]["session_id"] == "sess-2" and out["slot"]["runtime"] == "claude"
    assert out["artifacts"] == [{"name": "report.pdf", "url": "http://h/artifacts/c/report.pdf?t=x"}]
    with rc.restore({}, None, None) as nothing:
        assert nothing is None and access.current_identity() is idlib.ANONYMOUS
