"""Password hashing: Argon2id for new hashes, migrated bcrypt verified and
upgraded, length rules, and no password in any message."""
from __future__ import annotations

import bcrypt
import pytest

from hubzoid.auth import passwords


def test_new_hashes_are_argon2id_and_verify():
    stored = passwords.hash_password("correct horse battery")
    assert stored.startswith("$argon2id$")
    assert "correct horse battery" not in stored
    assert passwords.verify("correct horse battery", stored)
    assert not passwords.verify("correct horse batterY", stored)
    ok, updated = passwords.verify_and_update("correct horse battery", stored)
    assert ok and updated is None  # current parameters: nothing to upgrade


def test_migrated_bcrypt_verifies_and_is_rehashed_to_argon2():
    legacy = bcrypt.hashpw(b"from-open-webui", bcrypt.gensalt(rounds=4)).decode()
    ok, updated = passwords.verify_and_update("from-open-webui", legacy)
    assert ok and updated and updated.startswith("$argon2id$")
    assert passwords.verify("from-open-webui", updated)
    assert passwords.verify_and_update("wrong-password", legacy) == (False, None)


def test_migrated_bcrypt_compares_the_first_72_bytes_like_open_webui():
    long = "x" * 80
    legacy = bcrypt.hashpw(long.encode()[:72], bcrypt.gensalt(rounds=4)).decode()
    ok, updated = passwords.verify_and_update(long, legacy)
    assert ok and updated.startswith("$argon2id$")
    # The upgraded hash covers the whole password, not only 72 bytes.
    assert not passwords.verify("x" * 72 + "y" * 8, updated)


def test_argon2_hashes_with_old_parameters_are_upgraded():
    from argon2 import PasswordHasher

    old = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=2).hash("old parameters")
    ok, updated = passwords.verify_and_update("old parameters", old)
    assert ok and updated and updated != old


@pytest.mark.parametrize("stored", [None, "", "not-a-hash", "$2b$12$tooshort"])
def test_missing_or_unknown_hashes_never_match(stored):
    assert passwords.verify_and_update("whatever-it-is", stored) == (False, None)


@pytest.mark.parametrize("value,message", [
    (None, "Enter a password."),
    ("", "Enter a password."),
    ("short", "Use a password of at least 8 characters."),
    ("x" * 1025, "Use a password of at most 1024 characters."),
])
def test_length_rules(value, message):
    with pytest.raises(passwords.PasswordRejected) as exc:
        passwords.check(value)
    assert exc.value.message == message
    assert exc.value.code == "invalid_password"
    if value:
        assert value not in exc.value.message


def test_limits_are_characters_and_accepted_at_the_edges():
    assert passwords.check("é" * 8) == "é" * 8
    assert passwords.check("a" * 1024)
    stored = passwords.hash_password("a" * 1024)
    assert passwords.verify("a" * 1024, stored)
    # Longer than the maximum never matches and is never hashed as given.
    assert passwords.verify_and_update("a" * 1025, stored) == (False, None)


def test_is_supported_hash():
    assert passwords.is_supported_hash(passwords.hash_password("abcdefgh"))
    assert passwords.is_supported_hash(bcrypt.hashpw(b"abcdefgh", bcrypt.gensalt(4)).decode())
    assert not passwords.is_supported_hash("plain")
    assert not passwords.is_supported_hash(None)
