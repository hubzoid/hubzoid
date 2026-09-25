"""The generic webhook surface: env parsing, auth (shared-secret + HMAC), the
default file sink, namespaced routing, and the end-to-end handler via a
TestClient. No real HTTP, no LLM — this surface never touches either."""
import hashlib
import hmac
import json

import pytest
from starlette.testclient import TestClient

from hubzoid.inbound.env import missing_webhook_vars, webhook_config_from_env
from hubzoid.inbound.harness import WhatsAppConfig, build_app
from hubzoid.inbound.webhook import WebhookConfig, make_file_sink


# --- env / config ---------------------------------------------------------
def test_missing_webhook_vars_reports_secret():
    assert missing_webhook_vars({}) == ["WEBHOOK_INBOUND_SECRET"]
    assert missing_webhook_vars({"WEBHOOK_INBOUND_SECRET": "s"}) == []


def test_config_none_without_secret(tmp_path):
    assert webhook_config_from_env({}, hub_dir=tmp_path) is None


def test_config_defaults_name_and_shared_secret(tmp_path):
    cfg = webhook_config_from_env({"WEBHOOK_INBOUND_SECRET": "s"}, hub_dir=tmp_path)
    assert cfg.name == "webhook"
    assert cfg.hmac is False
    assert cfg.secret == "s"
    assert cfg.sink is not None  # default file sink wired when hub_dir given


def test_config_slugifies_name_and_reads_hmac(tmp_path):
    cfg = webhook_config_from_env(
        {"WEBHOOK_INBOUND_SECRET": "s", "WEBHOOK_INBOUND_NAME": "Squad Cast!",
         "WEBHOOK_INBOUND_HMAC": "true"}, hub_dir=tmp_path)
    assert cfg.name == "squad-cast"
    assert cfg.hmac is True


# --- auth -----------------------------------------------------------------
def _headers_lower(d):
    # Starlette headers are case-insensitive; the config reads both cases.
    return d


def test_shared_secret_accepts_bearer():
    cfg = WebhookConfig(secret="s")
    assert cfg.authenticate(raw_body=b"{}", headers={"authorization": "Bearer s"}, query={})


def test_shared_secret_accepts_header_and_query():
    cfg = WebhookConfig(secret="s")
    assert cfg.authenticate(raw_body=b"{}", headers={"x-webhook-secret": "s"}, query={})
    assert cfg.authenticate(raw_body=b"{}", headers={}, query={"token": "s"})


def test_shared_secret_rejects_wrong_and_missing():
    cfg = WebhookConfig(secret="s")
    assert not cfg.authenticate(raw_body=b"{}", headers={"authorization": "Bearer nope"}, query={})
    assert not cfg.authenticate(raw_body=b"{}", headers={}, query={})


def test_hmac_mode_verifies_body_signature():
    cfg = WebhookConfig(secret="key", hmac=True)
    body = b'{"event":"down"}'
    good = "sha256=" + hmac.new(b"key", body, hashlib.sha256).hexdigest()
    assert cfg.authenticate(raw_body=body, headers={"x-signature-256": good}, query={})
    # A shared-secret carrier is NOT accepted in HMAC mode.
    assert not cfg.authenticate(raw_body=body, headers={"authorization": "Bearer key"}, query={})
    # Wrong signature (right secret, tampered body) fails.
    assert not cfg.authenticate(raw_body=b"tampered", headers={"x-signature-256": good}, query={})


# --- default file sink ----------------------------------------------------
def test_file_sink_writes_event_json(tmp_path):
    sink = make_file_sink(tmp_path, "squadcast")
    sink({"surface": "webhook", "body": {"event": "down"}})
    inbox = tmp_path / ".inbound" / "webhooks" / "squadcast"
    files = list(inbox.glob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text())
    assert stored["body"] == {"event": "down"}


# --- end to end via the harness ------------------------------------------
def _app(tmp_path, cfg, slug="myhub"):
    return build_app(hub_dir=tmp_path, bridge_url="http://x/v1", api_key="k",
                     model="m", resolver=None, slug=slug, webhook=cfg)


def test_route_is_namespaced_by_slug_and_name(tmp_path):
    events = []
    cfg = WebhookConfig(secret="s", name="squadcast", sink=events.append)
    client = TestClient(_app(tmp_path, cfg, slug="acme"))
    r = client.post("/webhooks/acme/squadcast",
                    headers={"Authorization": "Bearer s"}, json={"event": "down"})
    assert r.status_code == 200 and r.text == "ok"
    assert events and events[0]["body"] == {"event": "down"}
    # The un-namespaced path does not exist.
    assert client.post("/webhooks/squadcast", headers={"Authorization": "Bearer s"},
                       json={}).status_code == 404


def test_bad_secret_is_rejected_before_sink(tmp_path):
    events = []
    cfg = WebhookConfig(secret="s", name="squadcast", sink=events.append)
    client = TestClient(_app(tmp_path, cfg, slug="acme"))
    r = client.post("/webhooks/acme/squadcast",
                    headers={"Authorization": "Bearer wrong"}, json={"event": "down"})
    assert r.status_code == 403
    assert events == []  # sink never ran


def test_non_json_body_is_kept_as_text(tmp_path):
    events = []
    cfg = WebhookConfig(secret="s", name="hook", sink=events.append)
    client = TestClient(_app(tmp_path, cfg, slug="h"))
    r = client.post("/webhooks/h/hook", headers={"Authorization": "Bearer s"},
                    content=b"plain text alert")
    assert r.status_code == 200
    assert events[0]["body"] == "plain text alert"


def test_sink_failure_returns_500_for_retry(tmp_path):
    def boom(_event):
        raise RuntimeError("disk full")
    cfg = WebhookConfig(secret="s", name="hook", sink=boom)
    client = TestClient(_app(tmp_path, cfg, slug="h"))
    r = client.post("/webhooks/h/hook", headers={"Authorization": "Bearer s"}, json={})
    assert r.status_code == 500


def test_whatsapp_and_webhook_coexist_under_one_slug(tmp_path):
    events = []
    wa = WhatsAppConfig(verify_token="VT", app_secret="SEC", token="T",
                        phone_number_id="P", send_text=lambda **k: {}, mark_read=lambda **k: {})
    cfg = WebhookConfig(secret="s", name="ci", sink=events.append)
    app = build_app(hub_dir=tmp_path, bridge_url="http://x/v1", api_key="k", model="m",
                    resolver=None, slug="h", whatsapp=wa, webhook=cfg)
    client = TestClient(app)
    # WhatsApp GET handshake is namespaced too.
    assert client.get("/webhooks/h/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "VT", "hub.challenge": "C"}).text == "C"
    # And the generic surface answers under the same slug.
    assert client.post("/webhooks/h/ci", headers={"Authorization": "Bearer s"},
                       json={"ok": 1}).status_code == 200
    assert events[0]["body"] == {"ok": 1}


# ---------------------------------------------------------------------------
# repeated deliveries, GitHub signatures, ?token=
# ---------------------------------------------------------------------------
def _post(client, body, **headers):
    return client.post("/webhooks/myhub/alerts", content=body,
                       headers={"Authorization": "Bearer s", "Content-Type": "application/json",
                                **headers})


def test_retried_delivery_with_the_same_id_is_stored_once(tmp_path):
    events = []
    client = TestClient(_app(tmp_path, WebhookConfig(secret="s", name="alerts", sink=events.append)))
    assert _post(client, b'{"n": 1}', **{"X-GitHub-Delivery": "d-1"}).text == "ok"
    assert _post(client, b'{"n": 1}', **{"X-GitHub-Delivery": "d-1"}).text == "duplicate"
    assert _post(client, b'{"n": 1}', **{"X-GitHub-Delivery": "d-2"}).text == "ok"
    assert len(events) == 2


def test_identical_body_without_an_id_is_a_repeat_only_briefly(tmp_path, monkeypatch):
    from hubzoid.inbound import harness, webhook

    events = []
    client = TestClient(_app(tmp_path, WebhookConfig(secret="s", name="alerts", sink=events.append)))
    now = [1_000_000.0]
    monkeypatch.setattr(harness.time, "time", lambda: now[0])
    assert _post(client, b'{"n": 1}').text == "ok"
    now[0] += 60
    assert _post(client, b'{"n": 1}').text == "duplicate"        # a quick retry
    now[0] += 3 * webhook.DUPLICATE_WINDOW_SECONDS
    assert _post(client, b'{"n": 1}').text == "ok"               # a genuine later event
    assert len(events) == 2


def test_failed_store_lets_the_retry_through(tmp_path):
    calls = []

    def flaky(event):
        calls.append(event)
        if len(calls) == 1:
            raise OSError("disk full")

    client = TestClient(_app(tmp_path, WebhookConfig(secret="s", name="alerts", sink=flaky)))
    assert _post(client, b'{"n": 1}', **{"X-Request-Id": "r-1"}).status_code == 500
    assert _post(client, b'{"n": 1}', **{"X-Request-Id": "r-1"}).text == "ok"
    assert len(calls) == 2


def test_github_signature_header_is_accepted(tmp_path):
    events = []
    client = TestClient(_app(tmp_path, WebhookConfig(secret="gh", name="alerts", hmac=True,
                                                     sink=events.append)))
    body = b'{"action": "opened"}'
    sig = "sha256=" + hmac.new(b"gh", body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/myhub/alerts", content=body,
                    headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200 and len(events) == 1
    bad = client.post("/webhooks/myhub/alerts", content=body,
                      headers={"X-Hub-Signature-256": "sha256=00", "Content-Type": "application/json"})
    assert bad.status_code == 403


def test_query_token_still_works_but_warns(tmp_path, caplog):
    import logging

    from hubzoid.inbound import webhook

    webhook._TOKEN_WARNED = False
    events = []
    client = TestClient(_app(tmp_path, WebhookConfig(secret="s", name="alerts", sink=events.append)))
    with caplog.at_level(logging.WARNING):
        r = client.post("/webhooks/myhub/alerts?token=s", json={"n": 1})
    assert r.status_code == 200 and len(events) == 1
    assert "?token=" in caplog.text


def test_a_store_cut_short_does_not_swallow_the_retry(tmp_path):
    """The process dies after the delivery is accepted but before its event is
    stored. The provider retries, and the retry must be stored, not dropped."""
    class Killed(BaseException):  # not an Exception: no handler cleanup runs
        pass

    calls = []

    def dies_first(event):
        calls.append(event)
        if len(calls) == 1:
            raise Killed()

    client = TestClient(_app(tmp_path, WebhookConfig(secret="s", name="alerts", sink=dies_first)))
    with pytest.raises(Killed):
        _post(client, b'{"n": 1}', **{"X-Request-Id": "r-1"})
    assert _post(client, b'{"n": 1}', **{"X-Request-Id": "r-1"}).text == "ok"
    assert len(calls) == 2
    assert _post(client, b'{"n": 1}', **{"X-Request-Id": "r-1"}).text == "duplicate"


def test_a_delivery_being_stored_elsewhere_is_retried_not_dropped(tmp_path):
    from hubzoid.inbound.dedup import Dedup

    events = []
    client = TestClient(_app(tmp_path, WebhookConfig(secret="s", name="alerts", sink=events.append)))
    with Dedup(tmp_path / ".inbound" / "dedup").holding("alerts:id:r-1") as held:
        assert held
        r = _post(client, b'{"n": 1}', **{"X-Request-Id": "r-1"})
        assert r.status_code == 503 and events == []
    assert _post(client, b'{"n": 1}', **{"X-Request-Id": "r-1"}).text == "ok"
    assert _post(client, b'{"n": 1}', **{"X-Request-Id": "r-1"}).text == "duplicate"
    assert len(events) == 1
