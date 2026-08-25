"""The generic webhook surface: env parsing, auth (shared-secret + HMAC), the
default file sink, namespaced routing, and the end-to-end handler via a
TestClient. No real HTTP, no LLM — this surface never touches either."""
import hashlib
import hmac
import json

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
    client = TestClient(_app(tmp_path, cfg, slug="ishahub"))
    r = client.post("/webhooks/ishahub/squadcast",
                    headers={"Authorization": "Bearer s"}, json={"event": "down"})
    assert r.status_code == 200 and r.text == "ok"
    assert events and events[0]["body"] == {"event": "down"}
    # The un-namespaced path does not exist.
    assert client.post("/webhooks/squadcast", headers={"Authorization": "Bearer s"},
                       json={}).status_code == 404


def test_bad_secret_is_rejected_before_sink(tmp_path):
    events = []
    cfg = WebhookConfig(secret="s", name="squadcast", sink=events.append)
    client = TestClient(_app(tmp_path, cfg, slug="ishahub"))
    r = client.post("/webhooks/ishahub/squadcast",
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
