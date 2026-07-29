"""The inbound harness end to end: verify -> dedup -> roster gate -> dispatch ->
send. Uses Starlette's TestClient with a real signature and injected fake
dispatch/send so no real HTTP or LLM is touched."""
import hashlib
import hmac
import json

from starlette.testclient import TestClient

from hubzoid.inbound.harness import Messages, TelegramConfig, WhatsAppConfig, build_app
from hubzoid.telegram.enrollment import Bindings


def _roster(mapping):
    return lambda surface, handle: mapping.get(handle)


def _recorder():
    calls = []

    def fn(**kw):
        calls.append(kw)
        return {}

    fn.calls = calls
    return fn


def _dispatcher(reply="**Reply**"):
    calls = []

    def fn(**kw):
        calls.append(kw)
        return reply

    fn.calls = calls
    return fn


def _wa_app(tmp_path, *, resolver, send_text, dispatch_fn, mark_read=None):
    wa = WhatsAppConfig(verify_token="VT", app_secret="SEC", token="TKN",
                        phone_number_id="PNID", send_text=send_text,
                        mark_read=(mark_read or (lambda **kw: {})))
    return build_app(hub_dir=tmp_path, bridge_url="http://x/v1", api_key="k",
                     model="m", resolver=resolver, whatsapp=wa, dispatch_fn=dispatch_fn)


def _wa_post(payload):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"SEC", raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def _wa_text_payload(wa_id, mid, body):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "contacts": [{"profile": {"name": "Ravi"}, "wa_id": wa_id}],
        "messages": [{"from": wa_id, "id": mid, "type": "text", "text": {"body": body}}],
    }}]}]}


# --- WhatsApp -------------------------------------------------------------
def test_whatsapp_get_echoes_challenge(tmp_path):
    app = _wa_app(tmp_path, resolver=_roster({}), send_text=_recorder(), dispatch_fn=_dispatcher())
    r = TestClient(app).get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "VT", "hub.challenge": "CHAL"})
    assert r.status_code == 200 and r.text == "CHAL"


def test_whatsapp_bad_signature_is_rejected(tmp_path):
    app = _wa_app(tmp_path, resolver=_roster({}), send_text=_recorder(), dispatch_fn=_dispatcher())
    r = TestClient(app).post("/webhooks/whatsapp", content=b'{"x":1}',
                             headers={"X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 403


def test_whatsapp_known_sender_gets_rendered_reply(tmp_path):
    send = _recorder()
    disp = _dispatcher("**Reply**")
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": ["coordinator"]}})
    app = _wa_app(tmp_path, resolver=resolver, send_text=send, dispatch_fn=disp)
    raw, headers = _wa_post(_wa_text_payload("919800000001", "wamid.1", "hello"))
    r = TestClient(app).post("/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 200
    assert disp.calls[-1]["user_email"] == "ravi@isha.org"
    assert disp.calls[-1]["groups"] == ["coordinator"]
    assert send.calls[-1]["to"] == "919800000001"
    assert send.calls[-1]["text"] == "*Reply*"   # WhatsApp flavor applied


def test_whatsapp_marks_read_and_typing_before_dispatch(tmp_path):
    reads = []
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": []}})
    app = _wa_app(tmp_path, resolver=resolver, send_text=_recorder(), dispatch_fn=_dispatcher(),
                  mark_read=lambda **kw: reads.append(kw) or {})
    raw, headers = _wa_post(_wa_text_payload("919800000001", "wamid.MR", "hi"))
    TestClient(app).post("/webhooks/whatsapp", content=raw, headers=headers)
    assert reads and reads[-1]["message_id"] == "wamid.MR"
    assert reads[-1]["typing"] is True


def test_whatsapp_unknown_sender_gets_notice_and_no_dispatch(tmp_path):
    send = _recorder()
    disp = _dispatcher()
    app = _wa_app(tmp_path, resolver=_roster({}), send_text=send, dispatch_fn=disp)
    raw, headers = _wa_post(_wa_text_payload("910000000000", "wamid.2", "hi"))
    r = TestClient(app).post("/webhooks/whatsapp", content=raw, headers=headers)
    assert r.status_code == 200
    assert disp.calls == []                       # stranger never reaches the LLM
    assert send.calls[-1]["to"] == "910000000000"  # got a canned notice


def test_whatsapp_duplicate_delivery_dispatched_once(tmp_path):
    send = _recorder()
    disp = _dispatcher()
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": []}})
    app = _wa_app(tmp_path, resolver=resolver, send_text=send, dispatch_fn=disp)
    client = TestClient(app)
    raw, headers = _wa_post(_wa_text_payload("919800000001", "wamid.SAME", "hi"))
    client.post("/webhooks/whatsapp", content=raw, headers=headers)
    client.post("/webhooks/whatsapp", content=raw, headers=headers)
    assert len(disp.calls) == 1


# --- Telegram -------------------------------------------------------------
def _tg_app(tmp_path, *, resolver, send_message, dispatch_fn, bindings,
            send_chat_action=None, edit_message_text=None, stream=False, messages=None):
    tg = TelegramConfig(secret_token="TS", bot_token="BOT",
                        bindings=bindings, send_message=send_message,
                        send_chat_action=(send_chat_action or (lambda **kw: {})),
                        edit_message_text=(edit_message_text or (lambda **kw: {})),
                        stream=stream)
    return build_app(hub_dir=tmp_path, bridge_url="http://x/v1", api_key="k",
                     model="m", resolver=resolver, telegram=tg, dispatch_fn=dispatch_fn,
                     messages=messages)


def test_telegram_bad_secret_rejected(tmp_path):
    app = _tg_app(tmp_path, resolver=_roster({}), send_message=_recorder(),
                  dispatch_fn=_dispatcher(), bindings=Bindings(tmp_path / "b"))
    r = TestClient(app).post("/webhooks/telegram", json={"update_id": 1},
                             headers={"X-Telegram-Bot-Api-Secret-Token": "WRONG"})
    assert r.status_code == 403


def test_telegram_contact_share_enrolls(tmp_path):
    send = _recorder()
    binds = Bindings(tmp_path / "b")
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": ["coordinator"]}})
    app = _tg_app(tmp_path, resolver=resolver, send_message=send,
                  dispatch_fn=_dispatcher(), bindings=binds)
    update = {"update_id": 5, "message": {
        "from": {"id": 42, "first_name": "Ravi"},
        "contact": {"phone_number": "919800000001", "user_id": 42}}}
    r = TestClient(app).post("/webhooks/telegram", json=update,
                             headers={"X-Telegram-Bot-Api-Secret-Token": "TS"})
    assert r.status_code == 200
    assert binds.phone_for("42") == "919800000001"
    assert send.calls[-1]["chat_id"] == "42"


def test_telegram_text_from_verified_gets_reply(tmp_path):
    send = _recorder()
    disp = _dispatcher("**Reply**")
    binds = Bindings(tmp_path / "b")
    binds.bind("42", "919800000001")
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": ["coordinator"]}})
    app = _tg_app(tmp_path, resolver=resolver, send_message=send, dispatch_fn=disp, bindings=binds)
    update = {"update_id": 6, "message": {"from": {"id": 42, "first_name": "Ravi"}, "text": "hi"}}
    r = TestClient(app).post("/webhooks/telegram", json=update,
                             headers={"X-Telegram-Bot-Api-Secret-Token": "TS"})
    assert r.status_code == 200
    assert disp.calls[-1]["user_email"] == "ravi@isha.org"
    assert send.calls[-1]["text"] == "<b>Reply</b>"   # Telegram HTML flavor


def test_telegram_verified_shows_typing_before_reply(tmp_path):
    actions = []
    binds = Bindings(tmp_path / "b")
    binds.bind("42", "919800000001")
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": ["coordinator"]}})
    app = _tg_app(tmp_path, resolver=resolver, send_message=_recorder(),
                  dispatch_fn=_dispatcher(), bindings=binds,
                  send_chat_action=lambda **kw: actions.append(kw) or {})
    update = {"update_id": 20, "message": {"from": {"id": 42, "first_name": "Ravi"}, "text": "hi"}}
    TestClient(app).post("/webhooks/telegram", json=update,
                         headers={"X-Telegram-Bot-Api-Secret-Token": "TS"})
    assert actions and actions[-1]["action"] == "typing"


def test_telegram_text_from_unverified_prompts_and_no_dispatch(tmp_path):
    send = _recorder()
    disp = _dispatcher()
    app = _tg_app(tmp_path, resolver=_roster({}), send_message=send,
                  dispatch_fn=disp, bindings=Bindings(tmp_path / "b"))
    update = {"update_id": 7, "message": {"from": {"id": 99, "first_name": "X"}, "text": "hi"}}
    r = TestClient(app).post("/webhooks/telegram", json=update,
                             headers={"X-Telegram-Bot-Api-Secret-Token": "TS"})
    assert r.status_code == 200
    assert disp.calls == []
    assert send.calls[-1]["chat_id"] == "99"


def test_telegram_streaming_sends_placeholder_then_edits_with_final(tmp_path):
    sends, edits = [], []
    binds = Bindings(tmp_path / "b")
    binds.bind("42", "919800000001")
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": []}})

    def streaming_dispatch(**kw):
        cb = kw.get("on_delta")
        for tok in ["**Hel", "lo** ", "there"]:
            if cb:
                cb(tok)
        return "**Hello** there"

    app = _tg_app(tmp_path, resolver=resolver,
                  send_message=lambda **kw: sends.append(kw) or {"result": {"message_id": 77}},
                  dispatch_fn=streaming_dispatch, bindings=binds, stream=True,
                  edit_message_text=lambda **kw: edits.append(kw) or {})
    update = {"update_id": 30, "message": {"from": {"id": 42, "first_name": "Ravi"}, "text": "hi"}}
    TestClient(app).post("/webhooks/telegram", json=update,
                         headers={"X-Telegram-Bot-Api-Secret-Token": "TS"})
    assert sends and sends[0]["text"] == "…"                    # placeholder first
    assert edits and edits[-1]["message_id"] == 77
    assert edits[-1]["text"] == "<b>Hello</b> there"            # final, rendered to HTML


def test_second_turn_carries_prior_history(tmp_path):
    # Slack/OWUI parity: the 2nd message sends the full array (prior user +
    # assistant, then the new user turn) so the agent has memory.
    disp = _dispatcher("The colour is blue.")
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": []}})
    app = _wa_app(tmp_path, resolver=resolver, send_text=_recorder(), dispatch_fn=disp)
    client = TestClient(app)
    raw1, h1 = _wa_post(_wa_text_payload("919800000001", "wamid.T1", "My colour is blue."))
    client.post("/webhooks/whatsapp", content=raw1, headers=h1)
    raw2, h2 = _wa_post(_wa_text_payload("919800000001", "wamid.T2", "What colour did I say?"))
    client.post("/webhooks/whatsapp", content=raw2, headers=h2)
    second = disp.calls[-1]["messages"]
    assert len(second) == 3
    assert second[0] == {"role": "user", "content": "My colour is blue."}
    assert second[1]["role"] == "assistant"
    assert second[-1] == {"role": "user", "content": "What colour did I say?"}


def test_history_isolated_between_two_senders(tmp_path):
    disp = _dispatcher("ok")
    resolver = _roster({
        "919800000001": {"email": "a@isha.org", "groups": []},
        "919800000002": {"email": "b@isha.org", "groups": []},
    })
    app = _wa_app(tmp_path, resolver=resolver, send_text=_recorder(), dispatch_fn=disp)
    client = TestClient(app)
    raw1, h1 = _wa_post(_wa_text_payload("919800000001", "wamid.A1", "my secret is BANANA47"))
    client.post("/webhooks/whatsapp", content=raw1, headers=h1)
    raw2, h2 = _wa_post(_wa_text_payload("919800000002", "wamid.B1", "what is the secret?"))
    client.post("/webhooks/whatsapp", content=raw2, headers=h2)
    # sender B's dispatch never sees sender A's message
    b_messages = disp.calls[-1]["messages"]
    assert b_messages == [{"role": "user", "content": "what is the secret?"}]


def test_messages_from_env_overrides_and_keeps_defaults():
    m = Messages.from_env({"INBOUND_MSG_NOT_REGISTERED": "Custom denial.",
                           "INBOUND_MSG_VERIFIED": ""})
    assert m.not_registered == "Custom denial."
    assert m.verified == "You are verified. How can I help?"   # blank override ignored


def test_env_overridden_not_registered_is_used_by_whatsapp(tmp_path):
    send = _recorder()
    wa = WhatsAppConfig(verify_token="VT", app_secret="SEC", token="TKN",
                        phone_number_id="PNID", send_text=send)
    app = build_app(hub_dir=tmp_path, bridge_url="http://x/v1", api_key="k", model="m",
                    resolver=_roster({}), whatsapp=wa, dispatch_fn=_dispatcher(),
                    messages=Messages(not_registered="You are not on the list."))
    raw, headers = _wa_post(_wa_text_payload("910000000000", "wamid.Z", "hi"))
    TestClient(app).post("/webhooks/whatsapp", content=raw, headers=headers)
    assert send.calls[-1]["text"] == "You are not on the list."
