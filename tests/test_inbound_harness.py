"""The inbound harness end to end: verify -> dedup -> roster gate -> dispatch ->
send. Uses Starlette's TestClient with a real signature and injected fake
dispatch/send so no real HTTP or LLM is touched."""
import hashlib
import hmac
import json
import threading
import time

from starlette.testclient import TestClient

from hubzoid.inbound.harness import (
    Messages,
    TelegramConfig,
    WhatsAppConfig,
    _chat_lock,
    build_app,
)
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


def _wa_image_payload(wa_id, mid, media_id, caption=None):
    image = {"id": media_id, "mime_type": "image/jpeg"}
    if caption is not None:
        image["caption"] = caption
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp",
        "contacts": [{"profile": {"name": "Ravi"}, "wa_id": wa_id}],
        "messages": [{"from": wa_id, "id": mid, "type": "image", "image": image}],
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


def test_whatsapp_image_ingests_and_stitches_marker_into_dispatch(tmp_path):
    send = _recorder()
    disp = _dispatcher("ok")
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": []}})
    seen = []

    def fake_ingest(*, surface, token, media, chat_id):
        seen.append((surface, token, [r.key for r in media], chat_id))
        return ["[Image: image-MID7.jpg]  (attached image, shown to you directly)"]

    wa = WhatsAppConfig(verify_token="VT", app_secret="SEC", token="TKN",
                        phone_number_id="PNID", send_text=send)
    app = build_app(hub_dir=tmp_path, bridge_url="http://x/v1", api_key="k", model="m",
                    resolver=resolver, whatsapp=wa, dispatch_fn=disp,
                    ingest_media_fn=fake_ingest)
    raw, headers = _wa_post(_wa_image_payload("919800000001", "wamid.IMG", "MID7", "look here"))
    TestClient(app).post("/webhooks/whatsapp", content=raw, headers=headers)
    # ingest was called with the right surface/token/media/chat_id
    assert seen[-1] == ("whatsapp", "TKN", ["MID7"], "whatsapp-919800000001")
    # the dispatched user turn carries the image marker AND the caption
    content = disp.calls[-1]["messages"][-1]["content"]
    assert "[Image: image-MID7.jpg]" in content
    assert "look here" in content


def test_whatsapp_empty_reply_sends_fallback_not_silence(tmp_path):
    # Code-review #4: a blank rendered reply used to leave the user in silence.
    send = _recorder()
    disp = _dispatcher("")   # renders to nothing
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": []}})
    app = _wa_app(tmp_path, resolver=resolver, send_text=send, dispatch_fn=disp)
    raw, headers = _wa_post(_wa_text_payload("919800000001", "wamid.E", "hi"))
    TestClient(app).post("/webhooks/whatsapp", content=raw, headers=headers)
    assert send.calls, "an empty reply must still acknowledge the user"
    assert send.calls[-1]["text"] == Messages().no_response


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


def test_telegram_photo_ingests_and_stitches_marker_into_dispatch(tmp_path):
    send = _recorder()
    disp = _dispatcher("ok")
    binds = Bindings(tmp_path / "b")
    binds.bind("42", "919800000001")
    resolver = _roster({"919800000001": {"email": "ravi@isha.org", "groups": []}})
    seen = []

    def fake_ingest(*, surface, token, media, chat_id):
        seen.append((surface, token, [r.key for r in media], chat_id))
        return ["[Image: photo-ub.jpg]  (attached image, shown to you directly)"]

    # Build directly (the _tg_app helper doesn't take an ingest fn).
    tg = TelegramConfig(secret_token="TS", bot_token="BOT", bindings=binds,
                        send_message=send)
    app = build_app(hub_dir=tmp_path, bridge_url="http://x/v1", api_key="k", model="m",
                    resolver=resolver, telegram=tg, dispatch_fn=disp,
                    ingest_media_fn=fake_ingest)
    update = {"update_id": 40, "message": {
        "from": {"id": 42, "first_name": "Ravi"}, "caption": "my receipt",
        "photo": [{"file_id": "big", "file_unique_id": "ub"}]}}
    TestClient(app).post("/webhooks/telegram", json=update,
                         headers={"X-Telegram-Bot-Api-Secret-Token": "TS"})
    assert seen[-1] == ("telegram", "BOT", ["big"], "telegram-42")
    content = disp.calls[-1]["messages"][-1]["content"]
    assert "[Image: photo-ub.jpg]" in content
    assert "my receipt" in content


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


# --- Per-chat serialization (concurrency) --------------------------------
def test_chat_lock_same_id_serializes():
    # Two turns for the same chat must not overlap — the second sees the first.
    events = []

    def work(tag):
        with _chat_lock("lock-test-X"):
            events.append(("enter", tag))
            time.sleep(0.1)
            events.append(("exit", tag))

    a = threading.Thread(target=work, args=("a",))
    b = threading.Thread(target=work, args=("b",))
    a.start(); b.start(); a.join(); b.join()
    # whichever ran first fully completed before the other entered (no interleave)
    assert events in (
        [("enter", "a"), ("exit", "a"), ("enter", "b"), ("exit", "b")],
        [("enter", "b"), ("exit", "b"), ("enter", "a"), ("exit", "a")],
    )


def test_chat_lock_different_ids_do_not_block():
    held = _chat_lock("lock-test-A")
    assert held.acquire(timeout=1)
    try:
        other = _chat_lock("lock-test-B")
        assert other.acquire(timeout=0.5) is True  # a different chat is never blocked
        other.release()
    finally:
        held.release()


def test_whatsapp_handler_waits_for_chat_lock(tmp_path):
    # Prove the WhatsApp handler dispatches only after acquiring the chat lock:
    # hold it, fire a message, and confirm no dispatch happens until we release.
    disp = _dispatcher("ok")
    resolver = _roster({"918888888888": {"email": "a@isha.org", "groups": []}})
    app = _wa_app(tmp_path, resolver=resolver, send_text=_recorder(), dispatch_fn=disp)
    client = TestClient(app)
    lk = _chat_lock("whatsapp-918888888888")
    assert lk.acquire(timeout=1)
    done = threading.Event()

    def fire():
        raw, headers = _wa_post(_wa_text_payload("918888888888", "wamid.LK", "hi"))
        client.post("/webhooks/whatsapp", content=raw, headers=headers)
        done.set()

    t = threading.Thread(target=fire); t.start()
    try:
        time.sleep(0.25)
        assert disp.calls == []          # blocked on the lock -> no dispatch yet
        lk.release()
        assert done.wait(3)              # released -> handler proceeds
        assert len(disp.calls) == 1
    finally:
        t.join(timeout=3)


def test_telegram_handler_waits_for_chat_lock(tmp_path):
    disp = _dispatcher("ok")
    binds = Bindings(tmp_path / "b"); binds.bind("77777", "918888888888")
    resolver = _roster({"918888888888": {"email": "a@isha.org", "groups": []}})
    app = _tg_app(tmp_path, resolver=resolver, send_message=_recorder(),
                  dispatch_fn=disp, bindings=binds)
    client = TestClient(app)
    lk = _chat_lock("telegram-77777")
    assert lk.acquire(timeout=1)
    done = threading.Event()

    def fire():
        update = {"update_id": 999, "message": {"from": {"id": 77777, "first_name": "X"},
                                                "text": "hi"}}
        client.post("/webhooks/telegram", json=update,
                    headers={"X-Telegram-Bot-Api-Secret-Token": "TS"})
        done.set()

    t = threading.Thread(target=fire); t.start()
    try:
        time.sleep(0.25)
        assert disp.calls == []
        lk.release()
        assert done.wait(3)
        assert len(disp.calls) == 1
    finally:
        t.join(timeout=3)


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
