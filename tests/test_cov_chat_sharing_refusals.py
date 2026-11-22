"""Snapshot links and collaborators refused at each rule
(hubzoid.chat.sharing): sharing off, only the owner, a known audience type,
named links name real people, something finished to share, the message and
size limits, a default expiry; a preview publishes only while it is fresh,
the conversation is unchanged and public links are still allowed, and never
past the per-conversation limit; collaborators are people with a role, within
the limit, the owner never listed. Uses the chat app of
tests/test_chat_sharing.py; settings are replaced per test."""
from __future__ import annotations

import pytest

from hubzoid.chat import sharing
from hubzoid.chat.sharing import SharingSettings
from tests.test_chat_sharing import as_owner_conversation, client, hub  # noqa: F401


def setup(client, **settings):  # noqa: F811
    svc = client.app.state.chat.sharing
    cfg = SharingSettings(**settings).model_dump()
    svc.settings = lambda: cfg
    cid, out = as_owner_conversation(client)
    conv = client.app.state.chat.store.get_conversation(cid)
    return svc, conv, out, cfg


def refused(fn, *a, **kw):
    with pytest.raises(sharing.Err) as err:
        fn(*a, **kw)
    return err.value.status, err.value.code


def test_previews_are_refused_at_each_rule(client):  # noqa: F811
    svc, conv, out, cfg = setup(client)
    owner = conv["owner_id"]
    head = {"head_message_id": out["assistant_message_id"]}
    assert refused(svc.preview, conv, "u_someone_else", head) == (403, "permission_denied")
    assert refused(svc.preview, conv, owner, {**head, "audience": {"type": "team"}}) == (422, "validation_failed")
    assert refused(svc.preview, conv, owner, {**head, "audience": {"type": "public"}}) == (403, "public_disabled")
    assert refused(svc.preview, conv, owner, {**head, "audience": {"type": "named", "principals": ["nobody@x.org"]}}) \
        == (422, "principal_not_found")
    assert refused(svc.preview, conv, owner, {**head, "audience": {"type": "named", "principals": []}}) \
        == (422, "validation_failed")
    assert refused(svc.preview, conv, owner, {"head_message_id": "no-such-message"}) == (422, "empty_snapshot")
    cfg["max_snapshot_messages"] = 1
    assert refused(svc.preview, conv, owner, head) == (413, "snapshot_too_large")
    cfg["max_snapshot_messages"], cfg["max_snapshot_bytes"] = 10_000, 50
    assert refused(svc.preview, conv, owner, head) == (413, "snapshot_too_large")
    cfg["enabled"] = False
    assert refused(svc.preview, conv, owner, head) == (403, "feature_disabled")


def test_a_preview_takes_every_branch_and_a_default_expiry(client):  # noqa: F811
    svc, conv, out, cfg = setup(client, default_expiry_seconds=7200)
    pv = svc.preview(conv, conv["owner_id"], {"path_mode": "all_branches"})
    assert [m["role"] for m in pv["projection"]["messages"]] == ["user", "assistant"]
    assert pv["projection"]["expires_at"] > sharing._now() + 7000


def test_publishing_needs_a_fresh_unchanged_allowed_preview(client, monkeypatch):  # noqa: F811
    svc, conv, out, cfg = setup(client)
    owner = conv["owner_id"]
    body = {"head_message_id": out["assistant_message_id"]}

    def fresh():
        pv = svc.preview(conv, owner, body)
        return {"preview_id": pv["preview_id"], "digest": pv["digest"]}

    late = fresh()
    real_now = sharing._now
    monkeypatch.setattr(sharing, "_now", lambda: real_now() + sharing.PREVIEW_SECONDS + 1)
    assert refused(svc.publish, owner, late) == (409, "preview_expired")
    monkeypatch.setattr(sharing, "_now", real_now)

    changed = fresh()
    import sqlalchemy as sa

    from hubzoid.chat.store import conversations
    with svc.engine.begin() as c:
        c.execute(sa.update(conversations).where(conversations.c.id == conv["id"])
                  .values(graph_revision=conversations.c.graph_revision + 1))
    assert refused(svc.publish, owner, changed) == (409, "snapshot_changed")

    cfg["public_enabled"] = True
    public = svc.preview(conv, owner, {**body, "audience": {"type": "public"}})
    cfg["public_enabled"] = False                                      # turned off before publishing
    assert refused(svc.publish, owner, {"preview_id": public["preview_id"], "digest": public["digest"]}) \
        == (403, "public_disabled")

    cfg["max_active_shares_per_conversation"] = 1
    assert svc.publish(owner, fresh())["id"]
    assert refused(svc.publish, owner, fresh()) == (429, "limit_reached")


def test_collaborators_are_people_with_a_role_within_the_limit(client):  # noqa: F811
    svc, conv, _out, cfg = setup(client, max_live_members=1)
    owner = conv["owner_id"]
    assert refused(svc.set_collaborators, conv, "u_other", [], 0) == (403, "permission_denied")
    two = [{"principal_id": "ana@example.org", "role": "editor"}, {"principal_id": "ben@example.org", "role": "viewer"}]
    assert refused(svc.set_collaborators, conv, owner, two, 0) == (422, "member_limit")
    assert refused(svc.set_collaborators, conv, owner, [{"principal_id": "ana@example.org", "role": "admin"}], 0) \
        == (422, "validation_failed")
    assert refused(svc.set_collaborators, conv, owner,
                   [{"principal_type": "group", "principal_id": "finance", "role": "viewer"}], 0) == (422, "validation_failed")
    assert refused(svc.set_collaborators, conv, owner, [{"email": "nobody@x.org", "role": "viewer"}], 0) \
        == (422, "principal_not_found")
    out = svc.set_collaborators(conv, owner, [{"principal_id": owner, "role": "editor"}], 0)
    assert out["members"] == []                                        # the owner is never a collaborator
    cfg["enabled"] = False
    assert refused(svc.set_collaborators, conv, owner, [], 0) == (403, "feature_disabled")
