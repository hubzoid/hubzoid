"""Tests for the open-core license-key system (hubzoid/licensing.py).

The runtime ships an embedded ED25519 PUBLIC key; enterprise keys are signed
with the matching PRIVATE key held only by Hubzoid. Verification is offline.
No valid key => community tier => no enterprise features (fail-closed). A
tampered, wrong-key, or expired token grants nothing.
"""
from __future__ import annotations

from datetime import date

import pytest

from hubzoid import licensing


def _kp() -> tuple[str, str]:
    return licensing.generate_keypair()


def test_sign_then_verify_roundtrip_returns_payload():
    priv, pub = _kp()
    token = licensing.issue(
        {"customer": "Samarth", "tier": "enterprise", "features": ["scheduling"]}, priv
    )
    payload = licensing.verify(token, pub)
    assert payload["customer"] == "Samarth"
    assert payload["features"] == ["scheduling"]


def test_tampered_payload_is_rejected():
    priv, pub = _kp()
    token = licensing.issue({"customer": "Samarth", "tier": "enterprise"}, priv)
    _, sig = token.split(".", 1)
    # Keep the real signature but swap in a different (attacker) payload.
    forged_payload = licensing.issue(
        {"customer": "Attacker", "tier": "enterprise"}, priv
    ).split(".", 1)[0]
    with pytest.raises(licensing.InvalidLicense):
        licensing.verify(f"{forged_payload}.{sig}", pub)


def test_wrong_public_key_is_rejected():
    priv_a, _ = _kp()
    _, pub_b = _kp()
    token = licensing.issue({"customer": "X", "tier": "enterprise"}, priv_a)
    with pytest.raises(licensing.InvalidLicense):
        licensing.verify(token, pub_b)


def test_verify_without_public_key_raises():
    priv, _ = _kp()
    token = licensing.issue({"customer": "X", "tier": "enterprise"}, priv)
    with pytest.raises(licensing.InvalidLicense):
        licensing.verify(token, "")


def test_no_key_is_community_with_no_features(monkeypatch):
    monkeypatch.delenv("LICENSE_KEY", raising=False)
    lic = licensing.load_license(token=None)
    assert lic.tier == "community"
    assert lic.valid is True
    assert lic.has_feature("scheduling") is False


def test_valid_enterprise_license_grants_listed_features():
    priv, pub = _kp()
    token = licensing.issue(
        {
            "customer": "Samarth",
            "tier": "enterprise",
            "features": ["scheduling", "access-control"],
        },
        priv,
    )
    lic = licensing.load_license(token=token, public_key_b64=pub)
    assert lic.valid is True
    assert lic.tier == "enterprise"
    assert lic.customer == "Samarth"
    assert lic.has_feature("scheduling") is True
    assert lic.has_feature("access-control") is True
    assert lic.has_feature("multi-tenant") is False


def test_wildcard_feature_grants_everything():
    priv, pub = _kp()
    token = licensing.issue(
        {"customer": "Friend", "tier": "enterprise", "features": ["*"]}, priv
    )
    lic = licensing.load_license(token=token, public_key_b64=pub)
    assert lic.has_feature("anything-at-all") is True


def test_expired_license_is_invalid_and_grants_nothing():
    priv, pub = _kp()
    token = licensing.issue(
        {
            "customer": "Samarth",
            "tier": "enterprise",
            "features": ["scheduling"],
            "expiry": "2020-01-01",
        },
        priv,
    )
    lic = licensing.load_license(token=token, public_key_b64=pub, today=date(2026, 6, 19))
    assert lic.valid is False
    assert "expired" in lic.reason
    assert lic.has_feature("scheduling") is False


def test_perpetual_license_without_expiry_stays_valid():
    priv, pub = _kp()
    token = licensing.issue(
        {"customer": "Samarth", "tier": "enterprise", "features": ["scheduling"]}, priv
    )
    lic = licensing.load_license(token=token, public_key_b64=pub, today=date(2099, 1, 1))
    assert lic.valid is True
    assert lic.has_feature("scheduling") is True


def test_garbage_token_loads_as_invalid_does_not_raise():
    _, pub = _kp()
    lic = licensing.load_license(token="garbage.token", public_key_b64=pub)
    assert lic.valid is False
    assert lic.has_feature("scheduling") is False


def test_load_reads_license_key_and_pubkey_from_env(monkeypatch):
    priv, pub = _kp()
    token = licensing.issue(
        {"customer": "Samarth", "tier": "enterprise", "features": ["scheduling"]}, priv
    )
    monkeypatch.setenv("LICENSE_KEY", token)
    monkeypatch.setenv("HUBZOID_LICENSE_PUBKEY", pub)
    lic = licensing.load_license()
    assert lic.valid is True
    assert lic.customer == "Samarth"
    assert lic.has_feature("scheduling") is True
