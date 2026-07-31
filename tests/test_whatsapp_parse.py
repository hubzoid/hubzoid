"""Parse a Meta webhook payload into the shared InboundMessage shape.

Text and quick-reply/button messages become messages; delivery-status
webhooks and unsupported media types yield nothing (nothing to answer)."""
from hubzoid.whatsapp.parse import parse_messages


def _payload(msg, contacts=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "messaging_product": "whatsapp",
            "contacts": contacts or [],
            "messages": [msg],
        }, "field": "messages"}]}],
    }


def test_parse_text_message():
    p = _payload(
        {"from": "919800000001", "id": "wamid.A", "type": "text", "text": {"body": "hello"}},
        contacts=[{"profile": {"name": "Ravi"}, "wa_id": "919800000001"}],
    )
    msgs = parse_messages(p)
    assert len(msgs) == 1
    m = msgs[0]
    assert m.id == "wamid.A"
    assert m.handle == "919800000001"
    assert m.text == "hello"
    assert m.name == "Ravi"
    assert m.surface == "whatsapp"


def test_status_webhook_yields_no_messages():
    p = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "statuses": [{"id": "wamid.A", "status": "delivered"}],
    }, "field": "messages"}]}]}
    assert parse_messages(p) == []


def test_button_quick_reply_extracts_text():
    p = _payload({"from": "91x", "id": "wamid.B", "type": "button",
                  "button": {"text": "Yes", "payload": "YES"}})
    assert parse_messages(p)[0].text == "Yes"


def test_interactive_button_reply_extracts_title():
    p = _payload({"from": "91x", "id": "wamid.C", "type": "interactive",
                  "interactive": {"type": "button_reply",
                                  "button_reply": {"id": "1", "title": "Confirm"}}})
    assert parse_messages(p)[0].text == "Confirm"


def test_image_media_is_surfaced_as_attachment():
    # Media is no longer dropped: an image becomes an answerable message with a
    # MediaRef (the harness downloads it) and an empty text when there's no caption.
    p = _payload({"from": "91x", "id": "wamid.D", "type": "image", "image": {"id": "x"}})
    [m] = parse_messages(p)
    assert m.text == ""
    assert m.media[0].key == "x"


def test_unhandled_type_with_no_media_is_skipped():
    p = _payload({"from": "91x", "id": "wamid.L", "type": "location",
                  "location": {"latitude": 1.0, "longitude": 2.0}})
    assert parse_messages(p) == []


def test_whitespace_only_text_is_skipped():
    p = _payload({"from": "91x", "id": "wamid.W", "type": "text", "text": {"body": "   "}})
    assert parse_messages(p) == []


def test_non_string_body_does_not_crash():
    p = _payload({"from": "91x", "id": "wamid.N", "type": "text", "text": {"body": 12345}})
    assert parse_messages(p) == []


def test_malformed_payloads_return_empty():
    assert parse_messages({}) == []
    assert parse_messages({"entry": [{}]}) == []
    assert parse_messages({"entry": [{"changes": [{"value": {}}]}]}) == []
