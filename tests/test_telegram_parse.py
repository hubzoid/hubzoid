"""Parse a Telegram Update and classify it: normal text (dispatch to the agent),
/start (send the verify prompt), or a shared contact (enrollment). Only text
updates become an InboundMessage; the harness routes the rest."""
from hubzoid.telegram.parse import parse_update, to_inbound


def test_text_update_fields():
    u = {"update_id": 10, "message": {
        "message_id": 1,
        "from": {"id": 42, "first_name": "Ravi", "language_code": "en"},
        "chat": {"id": 42}, "text": "hello",
    }}
    p = parse_update(u)
    assert p.update_id == "10"
    assert p.kind == "text"
    assert p.handle == "42"
    assert p.text == "hello"
    assert p.name == "Ravi"
    assert p.language_code == "en"


def test_start_command_classified():
    u = {"update_id": 11, "message": {"from": {"id": 42, "first_name": "Ravi"}, "text": "/start abc"}}
    assert parse_update(u).kind == "start"


def test_contact_share_classified():
    u = {"update_id": 12, "message": {
        "from": {"id": 42, "first_name": "Ravi"},
        "contact": {"phone_number": "+919800000001", "user_id": 42},
    }}
    p = parse_update(u)
    assert p.kind == "contact"
    assert p.contact_phone == "+919800000001"
    assert p.contact_user_id == "42"
    assert p.handle == "42"


def test_non_message_update_is_none():
    assert parse_update({"update_id": 13, "callback_query": {}}) is None


def test_malformed_update_is_none():
    assert parse_update({}) is None


def test_to_inbound_from_text_update():
    p = parse_update({"update_id": 10, "message": {
        "from": {"id": 42, "first_name": "Ravi"}, "text": "hello"}})
    m = to_inbound(p)
    assert m.id == "10"
    assert m.surface == "telegram"
    assert m.handle == "42"
    assert m.text == "hello"
    assert m.name == "Ravi"
