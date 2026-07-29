"""Telegram authenticity: Telegram echoes the secret we set via setWebhook in
the X-Telegram-Bot-Api-Secret-Token header on every POST. Compare it. Fail
closed if either side is missing (a hub with no secret configured rejects)."""
from hubzoid.telegram.verify import verify_secret


def test_matching_secret_passes():
    assert verify_secret("abc123", "abc123") is True


def test_mismatched_secret_fails():
    assert verify_secret("abc123", "different") is False


def test_missing_header_fails():
    assert verify_secret(None, "abc123") is False


def test_non_ascii_header_fails_closed():
    # A hostile non-ASCII header must return False, not raise TypeError.
    assert verify_secret("café", "abc123") is False


def test_unconfigured_expected_fails_closed():
    assert verify_secret("abc123", "") is False
    assert verify_secret(None, None) is False
