"""Phone normalization — the load-bearing cleanup that lets an inbound handle
(Meta wa_id, Telegram contact) match a roster row regardless of formatting."""
from hubzoid.inbound.normalize import normalize_phone


def test_strips_plus_spaces_and_punctuation_to_digits():
    assert normalize_phone("+91 98000-00001") == "919800000001"


def test_meta_waid_matches_prettified_roster_value():
    # Meta sends country-code + number with no plus; the roster may be prettified.
    # Both must normalize to the same key or every lookup silently misses.
    assert normalize_phone("919800000001") == normalize_phone("+91 (98000) 00001")


def test_empty_or_none_becomes_empty_string():
    assert normalize_phone("") == ""
    assert normalize_phone(None) == ""


def test_already_canonical_is_unchanged():
    assert normalize_phone("919800000001") == "919800000001"
