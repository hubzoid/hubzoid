"""Helpers a workflow or hub tool calls (hubzoid.workflows.context): who it
acts for (`current_user`, `hub.user`), its private data folder, secrets, and
checking a model's JSON against a response model. No engine, no model."""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from hubzoid import access, db
from hubzoid.structured import ModelOutputError
from hubzoid.workflows import context
from hubzoid.workflows.identity import LEGACY_SOURCE, RunIdentity


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_HUB_DIR", "HUBZOID_DATA_DIR", "ORDERS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    access._stores.clear()
    h = tmp_path / "desk"
    h.mkdir()
    (h / "AGENTS.md").write_text("---\nname: desk\n---\nx")
    return h


class Answer(BaseModel):
    total: int


def test_model_json_is_checked_after_the_call():
    assert context._validated({"a": 1}, None, "raw") == {"a": 1}
    with pytest.raises(ModelOutputError, match="not an object") as e:
        context._validated(["a"], None, '["a"]')
    assert e.value.raw == '["a"]'
    assert context._validated({"total": 3}, Answer, "raw").total == 3
    with pytest.raises(ModelOutputError, match="did not match Answer"):
        context._validated({"total": "many"}, Answer, '{"total": "many"}')


def test_data_dir_is_private_to_the_hub(hub, monkeypatch):
    with pytest.raises(RuntimeError, match="No hub here"):
        context.data_dir()
    path = context.data_dir(hub)
    assert path.is_dir() and path.name == "data" and ".hubzoid" in path.parts
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    assert context.data_dir() == path


def test_current_user_outside_a_run(hub):
    gs = access.store_for(hub)
    gs.grant("ana@example.org", "desk", "use_hub", actor="t")
    gs.set_attr("desk", "ana@example.org", "center", "north")
    assert context.current_user(hub) is None                       # nobody signed in
    with access.identity_scope(access.Identity.make(user="workflow:nightly", surface="workflow")):
        assert context.current_user(hub) is None                   # a workflow account is not a person
    with access.identity_scope(access.Identity.make(user="Ana@Example.org", surface="web")):
        user = context.current_user(hub)
        assert isinstance(user, context.RequestUser)
        assert user.id == user.email == "ana@example.org"
        assert user.can("use_hub") is True and user.can("manage_access") is False
        assert user.can("use_hub", "other-hub") is False
        assert user.attrs == {"center": "north"}


def test_hub_user_and_secrets_inside_a_run(hub, monkeypatch):
    access.store_for(hub).grant("ana@example.org", "desk", "use_hub", actor="t")
    access.store_for(hub).set_attr("desk", "ana@example.org", "center", "south")
    person = RunIdentity("ana@example.org", "ana@example.org", "test", "ana@example.org").to_dict()
    with context.run_scope(hub="desk", workflow="nightly", hub_dir=hub, engine=db.operational_engine(hub),
                           identity=person, run_id="run-1", work={}):
        user = context.current_user()
        assert isinstance(user, context.HubUser)
        assert user.id == "ana@example.org" and user.email == "ana@example.org"
        assert user.can("use_hub") is True and user.attrs == {"center": "south"}
        assert context.hub.secret("orders_token", "fallback") == "fallback"
        monkeypatch.setenv("ORDERS_TOKEN", "tok-1")
        assert context.hub.secret("orders_token") == "tok-1"

    access.store_for(hub).set_attr("desk", "workflow:nightly", "center", "never-read")
    service = RunIdentity("workflow:nightly", None, LEGACY_SOURCE).to_dict()   # not a person
    with context.run_scope(hub="desk", workflow="nightly", hub_dir=hub, engine=db.operational_engine(hub),
                           identity=service, run_id="run-2", work={}):
        assert context.hub.user.attrs == {}                        # no person, no personal attributes
