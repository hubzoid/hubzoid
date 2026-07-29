"""WhatsApp authenticity checks — the new security surface with no Slack analog.

GET handshake: Meta sends hub.mode/hub.verify_token/hub.challenge; echo the
challenge only if the token matches. POST: every body carries
X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app_secret, raw_body).
"""
import hashlib
import hmac

from hubzoid.whatsapp.verify import verify_challenge, verify_signature


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_challenge_echoed_when_token_matches():
    params = {"hub.mode": "subscribe", "hub.verify_token": "s3cret", "hub.challenge": "12345"}
    assert verify_challenge(params, "s3cret") == "12345"


def test_challenge_rejected_when_token_wrong():
    params = {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"}
    assert verify_challenge(params, "s3cret") is None


def test_challenge_rejected_when_mode_not_subscribe():
    params = {"hub.mode": "unsubscribe", "hub.verify_token": "s3cret", "hub.challenge": "12345"}
    assert verify_challenge(params, "s3cret") is None


def test_signature_valid():
    body = b'{"x":1}'
    assert verify_signature(body, _sign(body, "app_secret"), "app_secret") is True


def test_signature_invalid_hmac():
    assert verify_signature(b'{"x":1}', "sha256=deadbeef", "app_secret") is False


def test_signature_wrong_secret_fails():
    body = b'{"x":1}'
    assert verify_signature(body, _sign(body, "other"), "app_secret") is False


def test_signature_non_ascii_header_fails_closed():
    # A hostile non-ASCII signature must return False, not raise TypeError.
    assert verify_signature(b"{}", "sha256=café", "app_secret") is False


def test_signature_missing_or_malformed_header():
    assert verify_signature(b"{}", None, "app_secret") is False
    assert verify_signature(b"{}", "garbage", "app_secret") is False
    assert verify_signature(b"{}", "sha256=", "app_secret") is False
