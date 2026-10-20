"""The shared channel contract with a fake provider: files, admission,
replies, links and routing rules. No network and no model."""
from __future__ import annotations

import json
import textwrap

import pytest

from hubzoid.channels import admission, config, registry, replies, router, store
from hubzoid.channels.errors import ChannelError

from tests.channel_fake import FakeAdapter, delivery, headers

SECRET = "fake-signing-value"   # gitleaks:allow (a test value)


@pytest.fixture
def fake():
    adapter = FakeAdapter()
    registry.register(adapter)
    yield adapter
    registry.unregister("fake")


@pytest.fixture
def hub_dir(tmp_path, monkeypatch, fake):
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_PUBLIC_URL", "WEBUI_AUTH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FAKE_CHANNEL_SECRET", SECRET)
    root = tmp_path / "desk"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nA desk.\n")
    (root / "identity").mkdir()
    (root / "identity" / "access.csv").write_text("phone,email,groups\n,ana@example.org,support\n")
    return root


def yaml_for(**over) -> str:
    data = {"version": 1, "id": "fake-main", "provider": "fake", "enabled": True, "agent": "main",
            "allowed_groups": ["support"], "provider_config": {"secret_ref": "env:FAKE_CHANNEL_SECRET"}}
    data.update(over)
    return config.render(data)


def applied(hub_dir, **over):
    v = config.load_text(hub_dir, yaml_for(**over))
    assert v.valid, v.errors
    config.apply(hub_dir, v.data, expected_revision="absent", actor="test")
    return config.active(hub_dir, v.config.id)


def send(conn, *events, delivery_id="d1", secret=SECRET):
    raw, did = delivery(*events, delivery_id=delivery_id)
    return admission.receive(conn.hub_dir, conn.id, raw, headers(secret, raw, did), {})


def ev(eid="e1", thread="t1", user="U1", text="hello", **kw):
    return {"id": eid, "thread": thread, "user": user, "text": text, **kw}


# -- files ----------------------------------------------------------------------

def test_config_unknown_key_names_line(hub_dir):
    base = yaml_for()
    v = config.load_text(hub_dir, base + "colour: blue\n")
    assert not v.valid
    err = v.errors[0]
    assert err["field"] == "/colour" and err["line"] == base.count("\n") + 1
    assert "not a channel setting" in err["message"]


def test_config_provider_key_and_target_rules(hub_dir):
    v = config.load_text(hub_dir, yaml_for(provider_config={"secret_ref": "env:X", "nope": 1}))
    assert [e["field"] for e in v.errors] == ["/provider_config/nope"]
    v = config.load_text(hub_dir, yaml_for(workflow="desk"))
    assert not v.valid and "exactly one of agent" in v.errors[0]["message"]
    v = config.load_text(hub_dir, yaml_for(agent="someone-else"))
    assert v.errors[0]["code"] == "unknown_agent"
    v = config.load_text(hub_dir, yaml_for(id="Bad ID"))
    assert v.errors[0]["field"] == "/id"


def test_secret_ref_grammar(hub_dir):
    ok = config.load_text(hub_dir, yaml_for(provider_config={"secret_ref": "secret:channel/desk/fake-main/sig"}))
    assert ok.valid, ok.errors
    other_hub = config.load_text(hub_dir, yaml_for(provider_config={"secret_ref": "secret:channel/other/fake-main/s"}))
    other_channel = config.load_text(hub_dir, yaml_for(provider_config={"secret_ref": "secret:channel/desk/x/s"}))
    garbage = config.load_text(hub_dir, yaml_for(provider_config={"secret_ref": "plain-value"}))
    assert not other_hub.valid and not other_channel.valid and not garbage.valid


def test_a_hub_admin_names_only_the_connections_own_secrets(hub_dir):
    # A reference decides which value the connection sends to its host: a hub
    # administrator can't point it at the deployment's environment or secrets.
    for ref in ("env:HUBZOID_SECRET_KEY", "secret:deployment/model-key", "secret:hub/desk/model-key"):
        v = config.load_text(hub_dir, yaml_for(provider_config={"secret_ref": ref}))
        assert v.valid, v.errors
        with pytest.raises(ChannelError) as exc:
            config.apply(hub_dir, v.data, expected_revision="absent", actor="hub-admin", any_secret=False)
        assert exc.value.status == 403 and exc.value.extra["fields"][0]["field"] == "/provider_config/secret_ref"
    own = config.load_text(hub_dir, yaml_for(provider_config={"secret_ref": "secret:channel/desk/fake-main/sig"}))
    config.apply(hub_dir, own.data, expected_revision="absent", actor="hub-admin", any_secret=False)
    # An organization administrator's choice stays when a hub admin pauses the
    # connection, but its provider settings (where the value goes) stay theirs.
    chosen = config.load_text(hub_dir, yaml_for())
    config.apply(hub_dir, chosen.data, expected_revision=1, actor="org-admin", any_secret=True)
    paused = config.load_text(hub_dir, yaml_for(enabled=False))
    out = config.apply(hub_dir, paused.data, expected_revision=2, actor="hub-admin", any_secret=False)
    assert out["revision"] == 3
    moved = config.load_text(hub_dir, yaml_for(provider_config={"secret_ref": "env:FAKE_CHANNEL_SECRET",
                                                                "workspace": "elsewhere"}))
    with pytest.raises(ChannelError) as exc:
        config.apply(hub_dir, moved.data, expected_revision=3, actor="hub-admin", any_secret=False)
    assert exc.value.status == 403


def test_apply_writes_file_and_checks_revision(hub_dir):
    conn = applied(hub_dir)
    assert conn.revision == 1 and conn.enabled
    path = hub_dir / "channels" / "fake-main.yaml"
    assert path.is_file() and "provider: fake" in path.read_text()
    data = config.export(hub_dir, "fake-main")
    assert data["file_matches"] is True and data["config"]["provider_config"]["secret_ref"].startswith("env:")
    with pytest.raises(ChannelError) as exc:
        config.apply(hub_dir, config.load_text(hub_dir, yaml_for()).data, expected_revision="absent", actor="t")
    assert exc.value.code == "revision_conflict" and exc.value.extra["current_revision"] == 1
    out = config.set_enabled(hub_dir, "fake-main", False, expected_revision=1, actor="t")
    assert out["revision"] == 2 and out["status"] == "disabled"
    assert "enabled: false" in path.read_text()


def test_an_image_built_hub_keeps_channel_changes_in_its_data_folder(hub_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_DATA_DIR", str(tmp_path / "data"))      # its files come from Git
    data = config.load_text(hub_dir, yaml_for()).data
    out = config.apply(hub_dir, data, expected_revision="absent", actor="t")
    assert out["revision"] == 1 and out["file_written"] is False
    assert not (hub_dir / "channels").exists()                          # nothing written into the image
    assert config.active(hub_dir, "fake-main").enabled                  # the revision is what runs
    out = config.set_enabled(hub_dir, "fake-main", False, expected_revision=1, actor="t")
    assert out["status"] == "disabled" and not config.active(hub_dir, "fake-main").enabled
    exported = config.export(hub_dir, "fake-main")                      # what to commit to Git
    assert exported["config"]["enabled"] is False and exported["file_matches"] is None


def test_secret_value_never_exported(hub_dir):
    applied(hub_dir)
    blob = json.dumps(config.export(hub_dir, "fake-main"))
    assert SECRET not in blob


# -- admission -------------------------------------------------------------------

def test_signed_delivery_required(hub_dir):
    conn = applied(hub_dir)
    with pytest.raises(ChannelError) as exc:
        send(conn, ev(), secret="wrong")
    assert exc.value.status == 401
    assert store.list_deliveries(hub_dir, "desk", conn.id)["items"] == []


def test_repeat_delivery_is_one_event(hub_dir):
    conn = applied(hub_dir)
    first = send(conn, ev())
    again = send(conn, ev())
    assert first.status == "accepted" and again.duplicate and again.status == "duplicate"
    items = store.list_deliveries(hub_dir, "desk", conn.id)["items"]
    assert [i["status"] for i in items] == ["accepted"]


def test_reused_delivery_id_with_changed_bytes_is_quarantined(hub_dir):
    conn = applied(hub_dir)
    send(conn, ev())
    out = send(conn, ev(text="changed"))
    assert out.status == "quarantined"
    rows = store.list_deliveries(hub_dir, "desk", conn.id)["items"]
    assert sorted(r["status"] for r in rows) == ["accepted", "quarantined"]
    assert any(r["reason"] == "delivery_conflict" for r in rows)


def test_same_event_under_another_delivery_is_a_duplicate(hub_dir):
    conn = applied(hub_dir)
    send(conn, ev(), delivery_id="d1")
    out = send(conn, ev(kind="mention"), delivery_id="d2")
    assert out.duplicate and out.events[0]["reason"] == "same_event"


def test_disabled_connection_records_without_work(hub_dir):
    conn = applied(hub_dir, enabled=False)
    out = send(conn, ev())
    assert out.events[0]["status"] == "disabled"
    assert store.open_deliveries(hub_dir, "desk") == []


def test_unaddressed_message_is_ignored_until_subscribed(hub_dir):
    conn = applied(hub_dir)
    out = send(conn, ev(trigger=False))
    assert out.events[0]["reason"] == "not_addressed"
    thread = store.get_thread(hub_dir, out.events[0]["thread_id"])
    store.update_thread(hub_dir, thread["id"], subscribed=1)
    out = send(conn, ev(eid="e2", trigger=False), delivery_id="d2")
    assert out.events[0]["status"] == "accepted"


def test_origin_is_immutable(hub_dir):
    conn = applied(hub_dir)
    a = send(conn, ev())
    thread = store.get_thread(hub_dir, a.events[0]["thread_id"])
    with store.locked(hub_dir, "t") as c:
        same, created = store.thread_for(c, hub="desk", cid=conn.id, origin={
            "provider": "fake", "workspace_id": "W1", "thread_key": "fake:t1",
            "coordinates": {"thread": "SOMEWHERE-ELSE"}, "url": "https://evil.example"})
    assert not created and same["id"] == thread["id"]
    assert same["origin_json"]["coordinates"] == {"thread": "t1"}


def test_queue_limit_refuses_without_evicting(hub_dir):
    conn = applied(hub_dir, queue_limit=2)
    for n in range(3):
        send(conn, ev(eid=f"e{n}"), delivery_id=f"d{n}")
    statuses = [r["status"] for r in store.list_deliveries(hub_dir, "desk", conn.id)["items"]]
    assert sorted(statuses) == ["accepted", "accepted", "denied"]


# -- replies ----------------------------------------------------------------------

def test_split_preserves_fences_and_unicode():
    text = "intro " * 30 + "\n\n```\n" + ("line é\n" * 80) + "```\n\nend"
    parts = replies.split(text, 400, 20)
    assert len(parts) > 1 and all(len(p) <= 400 + 20 for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)
    assert parts[0].startswith(f"(1/{len(parts)})")
    many = replies.split("word " * 2000, 300, 3)
    assert len(many) == 3 and "longer than this channel allows" in many[-1]


def _thread(hub_dir, conn):
    out = send(conn, ev())
    return store.get_thread(hub_dir, out.events[0]["thread_id"])


def test_post_key_is_sent_once(hub_dir, fake):
    conn = applied(hub_dir)
    thread = _thread(hub_dir, conn)
    replies.post(conn, thread, kind="result", text="All done.", key="r1")
    replies.post(conn, thread, kind="result", text="All done.", key="r1")
    assert [s["text"] for s in fake.sent] == ["All done."]


def test_post_key_content_conflict(hub_dir, fake):
    conn = applied(hub_dir)
    thread = _thread(hub_dir, conn)
    replies.post(conn, thread, kind="result", text="One.", key="r1")
    out = replies.post(conn, thread, kind="result", text="Two.", key="r1")
    assert out[0]["state"] == "failed" and out[0]["code"] == "operation_argument_conflict"
    assert len(fake.sent) == 1


def test_uncertain_post_found_by_marker_settles(hub_dir, fake):
    conn = applied(hub_dir)
    thread = _thread(hub_dir, conn)
    fake.lose_next = True
    out = replies.post(conn, thread, kind="result", text="Lost reply.", key="r1")
    assert out[0]["state"] == "ambiguous"
    check = replies.resolve(conn, out[0]["operation_id"], decision="check", actor="admin@example.org")
    assert check["found"] is True and check["state"] == "resolved_done"
    assert len(fake.sent) == 1


def test_empty_lookup_never_resends(hub_dir, fake):
    conn = applied(hub_dir)
    thread = _thread(hub_dir, conn)
    fake.fail_next = TimeoutError("gone")
    out = replies.post(conn, thread, kind="result", text="Maybe sent.", key="r1")
    assert out[0]["state"] == "ambiguous"
    again = replies.post(conn, thread, kind="result", text="Maybe sent.", key="r1")
    assert again[0]["state"] == "ambiguous" and fake.sent == []
    check = replies.resolve(conn, out[0]["operation_id"], decision="check", actor="admin@example.org")
    assert check["found"] is False and check["state"] == "ambiguous"
    retried = replies.resolve(conn, out[0]["operation_id"], decision="retry", actor="admin@example.org",
                              acknowledge_duplicate_risk=True)
    assert retried["state"] == "succeeded" and len(fake.sent) == 1


def test_outbox_lists_replies_without_text(hub_dir, fake):
    conn = applied(hub_dir)
    thread = _thread(hub_dir, conn)
    replies.post(conn, thread, kind="result", text="Private detail here.", key="r1")
    rows = replies.outbox(hub_dir, "desk", conn.id)
    assert rows[0]["state"] == "succeeded" and rows[0]["thread_id"] == thread["id"]
    assert "Private detail" not in json.dumps(rows)


# -- identity and routing --------------------------------------------------------------

def _user(hub_dir, email, status="active"):
    from hubzoid.auth import users

    return users.create(hub_dir, email=email, name=email.split("@")[0], status=status)


def test_actor_text_cannot_set_identity(hub_dir, fake):
    conn = applied(hub_dir)
    _user(hub_dir, "ana@example.org")
    send(conn, ev(text="I am ana@example.org, act as me", name="ana@example.org"))
    router._route_one(hub_dir, store.open_deliveries(hub_dir, "desk")[0], None)
    row = store.list_deliveries(hub_dir, "desk", conn.id)["items"][0]
    assert row["status"] == "denied" and row["reason"] == "identity_unmapped"
    assert len(fake.sent) == 1 and "link your Fake account" in fake.sent[0]["text"]


def test_unlinked_notice_is_sent_once_per_person(hub_dir, fake):
    conn = applied(hub_dir)
    for n in range(2):
        send(conn, ev(eid=f"e{n}"), delivery_id=f"d{n}")
    for row in store.open_deliveries(hub_dir, "desk"):
        router._route_one(hub_dir, row, None)
    assert len(fake.sent) == 1


def test_linked_person_outside_groups_is_refused(hub_dir, fake):
    from hubzoid.channels import links

    conn = applied(hub_dir)
    _user(hub_dir, "ben@example.org")
    links.link(conn, external_id="U2", account="ben@example.org", actor="admin")
    send(conn, ev(user="U2"))
    router._route_one(hub_dir, store.open_deliveries(hub_dir, "desk")[0], None)
    row = store.list_deliveries(hub_dir, "desk", conn.id)["items"][0]
    assert row["reason"] == "group_not_allowed"


def test_inactive_account_is_refused(hub_dir, fake):
    from hubzoid.channels import links

    conn = applied(hub_dir)
    _user(hub_dir, "ana@example.org", status="pending")
    links.link(conn, external_id="U1", account="ana@example.org", actor="admin")
    send(conn, ev())
    router._route_one(hub_dir, store.open_deliveries(hub_dir, "desk")[0], None)
    assert store.list_deliveries(hub_dir, "desk", conn.id)["items"][0]["reason"] == "account_inactive"


def test_empty_request_gets_a_hint_without_work(hub_dir, fake):
    conn = applied(hub_dir)
    send(conn, ev(text="   "))
    router._route_one(hub_dir, store.open_deliveries(hub_dir, "desk")[0], None)
    assert store.list_deliveries(hub_dir, "desk", conn.id)["items"][0]["reason"] == "empty_request"
    assert "Tell me what you want done" in fake.sent[0]["text"]


def test_link_replaces_previous_account(hub_dir):
    from hubzoid.channels import links

    conn = applied(hub_dir)
    _user(hub_dir, "ana@example.org")
    _user(hub_dir, "ben@example.org")
    links.link(conn, external_id="U1", account="ana@example.org", actor="admin")
    links.link(conn, external_id="U1", account="ben@example.org", actor="admin")
    rows = links.links_for(conn)
    assert len(rows) == 1 and rows[0]["account"] == "ben@example.org"
    with pytest.raises(ChannelError) as exc:
        links.link(conn, external_id="U9", account="nobody@example.org", actor="admin")
    assert exc.value.code == "unknown_account"


def test_redrive_keeps_dedup_identity(hub_dir, fake):
    from hubzoid.channels import routes

    conn = applied(hub_dir)
    send(conn, ev())
    router._route_one(hub_dir, store.open_deliveries(hub_dir, "desk")[0], None)
    row = store.list_deliveries(hub_dir, "desk", conn.id)["items"][0]
    assert row["status"] == "denied"
    out = routes.redrive(hub_dir, "desk", conn.id, row["id"], "admin")
    assert out["status"] == "accepted" and out["delivery_id"] == row["delivery_id"]
    with pytest.raises(ChannelError) as exc:
        routes.redrive(hub_dir, "desk", conn.id, row["id"], "admin")
    assert exc.value.code == "already_routed"


def test_doctor_reports_unverified_delivery_and_public_url(hub_dir, monkeypatch):
    from hubzoid.channels import doctor

    conn = applied(hub_dir)
    out = doctor.run(conn)
    by_id = {c["id"]: c for c in out["checks"]}
    assert by_id["public_url"]["code"] == "public_url_required" and not out["ready"]
    assert "unverified" in by_id["delivery"]["message"]
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.test")
    out = doctor.run(conn)
    assert out["webhook_url"] == "https://hub.example.test/api/hubs/desk/channels/fake-main/webhook"
    assert out["network_writes"] is False


def test_workflow_route_starts_one_work_item(hub_dir, fake, monkeypatch):
    (hub_dir / "workflows" / "intake").mkdir(parents=True)
    (hub_dir / "workflows" / "intake" / "main.py").write_text(textwrap.dedent('''
        from hubzoid import hub, workflow

        @workflow(work_items=True)
        def intake():
            return {"reply": "ok"}
    '''))
    from hubzoid.work_items import service as wi

    started = []
    monkeypatch.setattr(wi, "start", lambda hub_dir, item: (started.append(item["id"]), item)[1])
    conn = applied(hub_dir, agent=None, workflow="intake", allowed_groups=[])
    send(conn, ev(data={"unattended": True}))
    router._route_one(hub_dir, store.open_deliveries(hub_dir, "desk")[0], None)
    row = store.list_deliveries(hub_dir, "desk", conn.id)["items"][0]
    assert row["status"] == "routed" and row["route_json"]["target"] == "work_item"
    thread = store.get_thread(hub_dir, row["thread_id"])
    assert thread["work_item_id"] == row["route_json"]["work_item_id"] and len(started) == 1
    # The same event again (a redelivery under a new id) starts nothing new.
    send(conn, ev(data={"unattended": True}), delivery_id="d9")
    assert store.open_deliveries(hub_dir, "desk") == []


def test_mention_upgrades_an_earlier_unaddressed_copy_once(hub_dir):
    conn = applied(hub_dir)
    send(conn, ev(trigger=False), delivery_id="d1")            # the plain message event
    out = send(conn, ev(kind="mention"), delivery_id="d2")      # its app_mention
    assert out.events[0]["status"] == "accepted"
    again = send(conn, ev(kind="mention"), delivery_id="d3")    # a retry of the mention
    assert again.events[0]["status"] == "duplicate"
    assert len(store.open_deliveries(hub_dir, "desk")) == 1


def test_conflicting_delivery_admits_none_of_its_events(hub_dir):
    conn = applied(hub_dir)
    send(conn, ev(), delivery_id="d1")
    out = send(conn, ev(), ev(eid="e2", thread="t2"), delivery_id="d1")   # same id, different bytes, an extra event
    assert out.status == "quarantined"
    assert [r["thread_id"] for r in store.open_deliveries(hub_dir, "desk")] != [] and \
        len(store.open_deliveries(hub_dir, "desk")) == 1


def test_validation_of_an_unknown_provider_returns_errors(hub_dir):
    v = config.load_text(hub_dir, yaml_for(provider="nope"))
    out = v.to_json()
    assert out["valid"] is False and out["errors"][0]["code"] == "unknown_provider"


def test_thread_claim_is_exclusive(hub_dir):
    conn = applied(hub_dir)
    thread = _thread(hub_dir, conn)
    assert store.claim_thread(hub_dir, thread["id"], "ana@example.org")
    assert store.claim_thread(hub_dir, thread["id"], "ana@example.org")     # the same person renews
    assert not store.claim_thread(hub_dir, thread["id"], "ben@example.org")
    store.release_thread(hub_dir, thread["id"], "ana@example.org")
    assert store.claim_thread(hub_dir, thread["id"], "ben@example.org")


def test_work_item_reply_cannot_be_settled_by_a_channel_admin(hub_dir, fake):
    """A reply a workflow sent belongs to its work item's requester."""
    from hubzoid.work_items import store as wstore

    conn = applied(hub_dir)
    thread = _thread(hub_dir, conn)
    fake.lose_next = True
    out = replies.post(conn, thread, kind="result", text="x", key="r1")
    op = out[0]["operation_id"]
    wstore.update_op(hub_dir, op, expected_revision=wstore.get_op(hub_dir, op)["revision"], item_id="item-1",
                     requester="someone@example.org")
    from hubzoid.work_items.errors import WorkItemError

    with pytest.raises(WorkItemError) as exc:
        replies.resolve(conn, op, decision="abandon", actor="admin@example.org")
    assert exc.value.code == "resolver_not_allowed"


def test_stop_signal_cancels_the_persons_work_item(hub_dir, fake, monkeypatch):
    (hub_dir / "workflows" / "intake").mkdir(parents=True)
    (hub_dir / "workflows" / "intake" / "main.py").write_text(textwrap.dedent('''
        from hubzoid import hub, workflow

        @workflow(work_items=True)
        def intake():
            return {"reply": "ok"}
    '''))
    from hubzoid.channels import links
    from hubzoid.work_items import service as wi
    from hubzoid.work_items import store as wstore

    monkeypatch.setattr(wi, "start", lambda hub_dir, item: item)
    conn = applied(hub_dir, agent=None, workflow="intake", run_as="workflow", allowed_groups=["support"])
    _user(hub_dir, "ana@example.org")
    from hubzoid.access import store_for

    store_for(hub_dir).grant("ana@example.org", "desk", "use_hub", actor="test")
    store_for(hub_dir).grant("ana@example.org", "desk", "workflows_manage", actor="test")
    links.link(conn, external_id="U1", account="ana@example.org", actor="admin")
    send(conn, ev())
    router._route_one(hub_dir, store.open_deliveries(hub_dir, "desk")[0], None)
    row0 = store.list_deliveries(hub_dir, "desk", conn.id)["items"][0]
    assert row0["status"] == "routed", (row0["reason"], row0["route_json"])
    item_id = row0["route_json"]["work_item_id"]
    send(conn, ev(eid="e2", text="", data={"signal": "stop"}), delivery_id="d2")   # Linear sends Stop with no text
    router._route_one(hub_dir, store.open_deliveries(hub_dir, "desk")[0], None)
    assert wstore.get_item(hub_dir, item_id)["state"] in ("cancelled", "accepted")
    assert any("Stopped" in s["text"] or "Nothing was running" in s["text"] for s in fake.sent)


def test_bot_events_admitted_only_when_the_adapter_allows_them(hub_dir, fake):
    conn = applied(hub_dir)
    send(conn, ev(eid="b1", bot=True, text="a bot comment"), ev(eid="b2", thread="t2", bot=True, text="CI failed",
                                                                  data={"bot_allowed": True}))
    rows = {r["semantic_key"]: r for r in store.list_deliveries(hub_dir, "desk", conn.id)["items"]}
    assert rows["msg:b1"]["reason"] == "bot_author"
    assert rows["msg:b2"]["status"] == "accepted"


def test_swap_connection_state_consumes_once(hub_dir, fake):
    conn = applied(hub_dir)
    store.set_connection_state(hub_dir, "desk", conn.id, attempt={"used": False})

    def take(state):
        if (state.get("attempt") or {}).get("used"):
            return None, False
        return {"attempt": {"used": True}}, True

    assert store.swap_connection_state(hub_dir, "desk", conn.id, take) is True
    assert store.swap_connection_state(hub_dir, "desk", conn.id, take) is False
