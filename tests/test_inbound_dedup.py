"""Dedup: webhook surfaces redeliver at-least-once, so each message id must be
processed exactly once. Atomic claim via exclusive file creation, persisted so
a restart mid-stream still drops the redelivery."""
from hubzoid.inbound.dedup import Dedup


def test_first_claim_is_new(tmp_path):
    assert Dedup(tmp_path).claim("wamid.A") is True


def test_second_claim_of_same_id_is_duplicate(tmp_path):
    d = Dedup(tmp_path)
    d.claim("wamid.A")
    assert d.claim("wamid.A") is False


def test_different_ids_are_independent(tmp_path):
    d = Dedup(tmp_path)
    assert d.claim("wamid.A") is True
    assert d.claim("wamid.B") is True


def test_claim_persists_across_instances(tmp_path):
    Dedup(tmp_path).claim("wamid.A")
    assert Dedup(tmp_path).claim("wamid.A") is False


def test_ids_with_awkward_characters_are_safe(tmp_path):
    d = Dedup(tmp_path)
    weird = "wamid.HBg/../../etc/passwd=="
    assert d.claim(weird) is True
    assert d.claim(weird) is False
