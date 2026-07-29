"""Per-chat conversation history in the hub database (SQLite by default).

Parity with Slack/OWUI (send the full array), stored in our own hz_ table.
Isolated per chat_id (a PII guarantee), capped (no months-of-chat overload),
with an optional TTL.
"""
from hubzoid.db import engine_for
from hubzoid.inbound.history import History


def _hist(tmp_path, **kw):
    return History(engine_for(tmp_path), **kw)


def test_new_chat_has_empty_history(tmp_path):
    assert _hist(tmp_path).load("whatsapp-1") == []


def test_append_and_load_roundtrip(tmp_path):
    h = _hist(tmp_path)
    h.append("c1", "user", "hi")
    h.append("c1", "assistant", "hello")
    assert h.load("c1") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_capped_to_max_messages(tmp_path):
    h = _hist(tmp_path, max_messages=2)
    h.append("c1", "user", "a")
    h.append("c1", "assistant", "b")
    h.append("c1", "user", "c")
    assert h.load("c1") == [
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]


def test_chats_are_isolated(tmp_path):
    h = _hist(tmp_path)
    h.append("telegram-42", "user", "my secret is BANANA47")
    assert h.load("telegram-99") == []


def test_history_persists_across_instances(tmp_path):
    _hist(tmp_path).append("c1", "user", "hi")
    assert _hist(tmp_path).load("c1") == [{"role": "user", "content": "hi"}]


def test_blank_content_is_not_stored(tmp_path):
    h = _hist(tmp_path)
    h.append("c1", "assistant", "   ")
    assert h.load("c1") == []


def test_ttl_drops_messages_older_than_window(tmp_path):
    h = _hist(tmp_path, ttl_seconds=100)
    h.append("c1", "user", "old", _now=1000)            # created at t=1000
    h.append("c1", "user", "recent", _now=1_000_000)    # now, cutoff=999900 -> 'old' pruned
    assert h.load("c1") == [{"role": "user", "content": "recent"}]
