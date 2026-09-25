"""Dedup: webhook surfaces redeliver at-least-once, so each message id must be
processed exactly once. Atomic claim via exclusive file creation, persisted so
a restart mid-stream still drops the redelivery."""
from hubzoid.inbound.dedup import Dedup


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


def test_holding_is_exclusive_across_instances(tmp_path):
    with Dedup(tmp_path).holding("hook:id:1") as first:
        with Dedup(tmp_path).holding("hook:id:1") as second:
            assert first is True and second is False
        with Dedup(tmp_path).holding("hook:id:2") as other:
            assert other is True


def test_a_hold_dropped_before_the_claim_leaves_the_id_open(tmp_path):
    """A crash mid-store drops the hold with no claim: the retry must be taken."""
    d = Dedup(tmp_path)
    with d.holding("hook:id:1") as held:
        assert held
    assert d.seen("hook:id:1") is False
    assert list(tmp_path.glob("*.lock"))  # kept, since no claim was made
    with d.holding("hook:id:1") as held:
        assert held
        d.claim("hook:id:1")
    assert d.seen("hook:id:1") is True
    assert not list(tmp_path.glob("*.lock"))  # claimed, so the lock file is gone
