"""The conversation store on SQLite and PostgreSQL (op_0010 schema)."""
from __future__ import annotations

import secrets

import pytest
from sqlalchemy import create_engine, text

from hubzoid import migrations
from hubzoid.chat import store as store_mod
from hubzoid.chat.store import ConversationStore, IdConflict, InvalidCursor


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path):
    if request.param == "sqlite":
        engine = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    else:
        base_url = request.getfixturevalue("postgres_url")
        name = "hz_chat_" + secrets.token_hex(4)
        admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))
        admin.dispose()
        engine = create_engine(base_url.rsplit("/", 1)[0] + "/" + name)
    migrations.upgrade(engine, "operational")
    yield ConversationStore(engine)
    engine.dispose()


def _conv(store, conv_id="c_first0001", owner="u_ana", hub="alpha", now=None, **kw):
    return store.create_conversation(conv_id=conv_id, owner_id=owner,
                                     owner_email=f"{owner}@example.org", hub=hub,
                                     agent="alpha-agent", now=now, **kw)


def _msg(store, mid, conv="c_first0001", parent=None, role="user", text_="hello", **kw):
    return store.insert_message(message_id=mid, conversation_id=conv, parent_id=parent, role=role,
                                content=[{"type": "text", "text": text_}], text=text_, **kw)


def test_create_get_and_owner_check(store):
    conv = _conv(store)
    assert conv["title_source"] == "pending" and conv["archived"] is False
    assert store.get_conversation("c_first0001")["agent"] == "alpha-agent"
    assert store.owned_conversation("c_first0001", "u_ana")["id"] == "c_first0001"
    assert store.owned_conversation("c_first0001", "u_ben") is None
    assert store.get_conversation("c_missing01") is None
    with pytest.raises(IdConflict):
        _conv(store, owner="u_ben")


def test_messages_tree_branch_and_latest_leaf(store):
    _conv(store)
    _msg(store, "m_user0001", now=1)
    _msg(store, "m_asst0001", parent="m_user0001", role="assistant", text_="hi", now=2)
    _msg(store, "m_user0002", parent="m_asst0001", text_="second", now=3)
    _msg(store, "m_user0002b", parent="m_asst0001", text_="edited second", now=4)
    branch = store.branch("c_first0001", "m_user0002b")
    assert [m["id"] for m in branch] == ["m_user0001", "m_asst0001", "m_user0002b"]
    assert branch[0]["content"] == [{"type": "text", "text": "hello"}]
    assert store.branch("c_first0001", "m_nothere1") == []
    assert store.latest_leaf("c_first0001") == "m_user0002b"
    assert [m["id"] for m in store.list_messages("c_first0001")] == [
        "m_user0001", "m_asst0001", "m_user0002", "m_user0002b"]


def test_message_ids_are_unique_across_conversations(store):
    _conv(store)
    _conv(store, conv_id="c_second001")
    _msg(store, "m_shared001")
    with pytest.raises(IdConflict):
        _msg(store, "m_shared001", conv="c_second001")


def test_update_message_and_sweep(store):
    _conv(store, hub="alpha")
    _conv(store, conv_id="c_other0001", hub="beta")
    _msg(store, "m_run00001", role="assistant", status="running")
    _msg(store, "m_run00002", conv="c_other0001", role="assistant", status="running")
    store.update_message("m_run00001", content=[{"type": "text", "text": "part"}], text="part")
    assert store.get_message("m_run00001")["content"] == [{"type": "text", "text": "part"}]
    assert store.sweep_running("alpha", error="interrupted") == 1
    swept = store.get_message("m_run00001")
    assert swept["status"] == "error" and swept["error"] == "interrupted"
    assert store.get_message("m_run00002")["status"] == "running"   # another hub's reply
    store.update_message("m_run00002", status="complete", usage={"input_tokens": 3}, model="m")
    done = store.get_message("m_run00002")
    assert done["usage"] == {"input_tokens": 3} and done["model"] == "m"


def test_list_filters_by_owner_and_archived(store):
    _conv(store, conv_id="c_ana00001", now=1)
    _conv(store, conv_id="c_ana00002", now=2)
    _conv(store, conv_id="c_ben00001", owner="u_ben", now=3)
    store.update_conversation("c_ana00002", archived=True)
    items, cursor = store.list_conversations("u_ana")
    assert [c["id"] for c in items] == ["c_ana00001"] and cursor is None
    items, _ = store.list_conversations("u_ana", archived=True)
    assert [c["id"] for c in items] == ["c_ana00002"] and items[0]["archived"] is True


def test_pagination_orders_by_activity_then_id(store):
    for n, ts in enumerate([5, 7, 7, 7, 1]):
        _conv(store, conv_id=f"c_page0000{n}", now=ts)
    seen = []
    cursor = None
    while True:
        items, cursor = store.list_conversations("u_ana", limit=2, cursor=cursor)
        seen += [c["id"] for c in items]
        if cursor is None:
            break
    assert seen == ["c_page00003", "c_page00002", "c_page00001", "c_page00000", "c_page00004"]
    with pytest.raises(InvalidCursor):
        store.list_conversations("u_ana", cursor="not-a-cursor")


def test_search_title_and_message_text(store):
    _conv(store, conv_id="c_search001", title="Budget review", now=1)
    _conv(store, conv_id="c_search002", now=2)
    _msg(store, "m_search01", conv="c_search002", text_="What is our PTO_policy for 100% remote staff?")
    _conv(store, conv_id="c_search003", owner="u_ben", title="budget for ben", now=3)
    assert [c["id"] for c in store.list_conversations("u_ana", q="BUDGET")[0]] == ["c_search001"]
    assert [c["id"] for c in store.list_conversations("u_ana", q="pto_policy")[0]] == ["c_search002"]
    # LIKE wildcards are literal
    assert [c["id"] for c in store.list_conversations("u_ana", q="100%")[0]] == ["c_search002"]
    assert store.list_conversations("u_ana", q="PTO%policy")[0] == []
    assert store.list_conversations("u_ana", q="_")[0][0]["id"] == "c_search002"


def test_titles_respect_the_source(store):
    _conv(store)
    assert store.set_title("c_first0001", "Draft", "pending", only_if_source=("pending",))
    store.update_conversation("c_first0001", title="Mine", title_source="user")
    assert not store.set_title("c_first0001", "Model title", "auto", only_if_source=("pending",))
    assert not store.set_title_source("c_first0001", "fallback", only_if_source=("pending",))
    assert store.get_conversation("c_first0001")["title"] == "Mine"


def test_touch_and_head(store):
    _conv(store, now=1)
    _msg(store, "m_head0001")
    store.touch("c_first0001", head_id="m_head0001", now=50)
    conv = store.get_conversation("c_first0001")
    assert conv["updated_at"] == 50 and conv["head_id"] == "m_head0001"


def test_shares_keep_one_link_per_conversation(store):
    _conv(store)
    first = store.save_share(conversation_id="c_first0001", owner_id="u_ana", title="T",
                             agent="alpha-agent", snapshot={"messages": [1]})
    again = store.save_share(conversation_id="c_first0001", owner_id="u_ana", title="T2",
                             agent="alpha-agent", snapshot={"messages": [1, 2]})
    assert again["id"] == first["id"]
    got = store.get_share(first["id"])
    assert got["snapshot"] == {"messages": [1, 2]} and got["title"] == "T2"
    assert store.share_for("c_first0001")["id"] == first["id"]
    assert store.delete_shares("c_first0001") == 1
    assert store.get_share(first["id"]) is None


def test_delete_removes_messages_and_shares(store):
    _conv(store)
    _msg(store, "m_del00001")
    store.save_share(conversation_id="c_first0001", owner_id="u_ana", title=None, agent=None,
                     snapshot={})
    assert store.delete_conversation("c_first0001")
    assert store.get_conversation("c_first0001") is None
    assert store.get_message("m_del00001") is None
    assert store.share_for("c_first0001") is None
    assert not store.delete_conversation("c_first0001")


def test_id_rules():
    assert store_mod.valid_message_id("m_abc-123_X")
    assert not store_mod.valid_message_id("short")
    assert not store_mod.valid_message_id("x" * 65)
    assert not store_mod.valid_message_id("has space1")
    assert store_mod.valid_conversation_id("c_abcdef12")
    # the per-chat folder name drops leading/trailing '-' and '_'
    assert not store_mod.valid_conversation_id("c_abcdef1_")
    assert not store_mod.valid_conversation_id("-abcdefgh")
    for _ in range(50):
        cid, mid = store_mod.new_id("c_"), store_mod.new_id("m_")
        assert store_mod.valid_conversation_id(cid) and store_mod.valid_message_id(mid)


def test_for_hub_uses_the_operational_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'shared.db'}")
    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    hub = tmp_path / "hub"
    hub.mkdir()
    s = store_mod.for_hub(hub)
    assert s is store_mod.for_hub(hub)
    assert str(s.engine.url).endswith("shared.db")
