"""1.x channel listeners (legacy Slack app, Telegram and WhatsApp) tell a person
in plain words when the bridge refuses or fails: never the exception, the
bridge's local URL or the bridge's own detail (which can name an email)."""
from __future__ import annotations

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from starlette.testclient import TestClient

from hubzoid import bridge_refusal
from hubzoid.slack import adapter as legacy_slack
from tests.test_inbound_harness import _roster, _tg_app, _wa_app, _wa_post, _wa_text_payload
from hubzoid.telegram.enrollment import Bindings

BRIDGE = "http://127.0.0.1:8000/v1/chat/completions"


def _refusal(status: int, detail: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", BRIDGE)
    response = httpx.Response(status, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(f"Client error '{status}' for url '{BRIDGE}'", request=request, response=response)


@pytest.mark.parametrize("status,detail,expected", [
    (403, "The 'ops' hub requires sign-in.", bridge_refusal.NO_ACCESS),
    (403, "You do not have access to the 'ops' hub. Signed in as ana@example.org.", bridge_refusal.NO_ACCESS),
    (403, "Your agent access is blocked. Contact your administrator.", bridge_refusal.BLOCKED),
    (401, "invalid api key", bridge_refusal.MISCONFIGURED.format(surface="Slack")),
    (404, "not found", bridge_refusal.MISCONFIGURED.format(surface="Slack")),
    (503, "draining", bridge_refusal.BUSY),
    (429, "", bridge_refusal.BUSY),
])
def test_each_refusal_has_its_words(status, detail, expected):
    assert bridge_refusal.notice_for(_refusal(status, detail), surface="slack") == expected


def test_an_unexpected_failure_gets_a_reference_only():
    text = bridge_refusal.notice_for(httpx.ConnectError(f"All connection attempts failed for {BRIDGE}"))
    assert text.startswith("I could not answer right now.") and "(ref " in text and "127.0.0.1" not in text


@settings(max_examples=60, deadline=None)
@given(status=st.sampled_from([400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504]),
       detail=st.text(max_size=80), host=st.sampled_from(["127.0.0.1", "localhost", "10.0.0.5", "hub.internal"]))
def test_no_notice_ever_leaks_the_url_the_error_or_the_detail(status, detail, host):
    request = httpx.Request("POST", f"http://{host}:8000/v1/chat/completions")
    exc = httpx.HTTPStatusError(f"error for url '{request.url}'", request=request,
                                response=httpx.Response(status, json={"detail": detail}, request=request))
    text = bridge_refusal.notice_for(exc, surface="telegram")
    assert host not in text and "http" not in text and "HTTPStatusError" not in text
    assert text in {bridge_refusal.NO_ACCESS, bridge_refusal.BLOCKED, bridge_refusal.BUSY,
                    bridge_refusal.MISCONFIGURED.format(surface="Telegram")} or text.startswith(
        "I could not answer right now.")


class _Slack:
    def __init__(self):
        self.updates, self.posts = [], []

    def chat_update(self, **kw):
        self.updates.append(kw)

    def chat_postMessage(self, **kw):
        self.posts.append(kw)


def test_legacy_slack_replaces_the_placeholder_with_the_notice():
    web = _Slack()
    legacy_slack._refused(web, "C1", "1.0", "2.0", _refusal(403, "requires sign-in"))
    assert web.updates == [{"channel": "C1", "ts": "2.0", "text": bridge_refusal.NO_ACCESS}]
    legacy_slack._refused(web, "C1", "1.0", None, _refusal(503))
    assert web.posts == [{"channel": "C1", "thread_ts": "1.0", "text": bridge_refusal.BUSY}]


def test_legacy_stream_reads_the_refusal_body():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"detail": "Your agent access is blocked."})))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        legacy_slack.stream_reply(bridge_url="http://127.0.0.1:8000/v1", api_key="k", model="m",
                                  messages=[{"role": "user", "content": "hi"}], on_delta=lambda d: None,
                                  http_client=client)
    assert bridge_refusal.notice_for(caught.value) == bridge_refusal.BLOCKED


def _refusing(**_kw):
    raise _refusal(403, "Signed in as ravi@example.org")


def test_whatsapp_refusal_is_told_not_dropped(tmp_path):
    sent = []
    app = _wa_app(tmp_path, resolver=_roster({"919800000001": {"email": "ravi@example.org"}}),
                  send_text=lambda **kw: sent.append(kw) or {}, dispatch_fn=_refusing)
    raw, headers = _wa_post(_wa_text_payload("919800000001", "wamid.R", "hello"))
    TestClient(app).post("/webhooks/whatsapp", content=raw, headers=headers)
    assert [s["text"] for s in sent] == [bridge_refusal.NO_ACCESS]


@pytest.mark.parametrize("stream", [False, True])
def test_telegram_refusal_replaces_the_placeholder_or_is_sent(tmp_path, stream):
    sends, edits = [], []
    binds = Bindings(tmp_path / "b")
    binds.bind("42", "919800000001")
    app = _tg_app(tmp_path, resolver=_roster({"919800000001": {"email": "ravi@example.org"}}),
                  send_message=lambda **kw: sends.append(kw) or {"result": {"message_id": 7}},
                  dispatch_fn=_refusing, bindings=binds, stream=stream,
                  edit_message_text=lambda **kw: edits.append(kw) or {})
    TestClient(app).post("/webhooks/telegram", json={"update_id": 9, "message": {"from": {"id": 42}, "text": "hi"}},
                         headers={"X-Telegram-Bot-Api-Secret-Token": "TS"})
    if stream:
        assert sends[0]["text"] == "…" and edits[-1]["text"] == bridge_refusal.NO_ACCESS
    else:
        assert [s["text"] for s in sends] == [bridge_refusal.NO_ACCESS]
