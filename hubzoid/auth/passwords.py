"""Password hashing for Hubzoid accounts.

Argon2id (through pwdlib) hashes every new password. Hashes migrated from Open
WebUI are bcrypt: they verify here and are replaced by an Argon2id hash the
next time the person signs in (``verify_and_update``).

Parameters follow the OWASP recommendation for Argon2id (19 MiB, 2 passes, 1
lane) rather than argon2-cffi's heavier default, so a burst of sign-ins cannot
exhaust memory. A small semaphore also caps how many hashes run at once per
process. Changing the parameters later is safe: ``verify_and_update`` rehashes
old hashes at the next sign-in.

Nothing here logs or returns a password, and error messages never repeat one.
"""
from __future__ import annotations

import os
import threading

import bcrypt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

MIN_LENGTH = 8
MAX_LENGTH = 1024

# bcrypt only ever read the first 72 bytes of a password. Open WebUI's hashes
# were made that way, so a migrated hash is checked the same way.
_BCRYPT_MAX_BYTES = 72


class PasswordRejected(ValueError):
    """A password that doesn't meet the rules. ``message`` is safe to show."""

    code = "invalid_password"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class _MigratedBcrypt(BcryptHasher):
    """Verifies bcrypt hashes from Open WebUI. Never used to hash."""

    def verify(self, password: str | bytes, hash: str | bytes) -> bool:
        raw = password.encode("utf-8") if isinstance(password, str) else password
        stored = hash.encode("utf-8") if isinstance(hash, str) else hash
        try:
            return bcrypt.checkpw(raw[:_BCRYPT_MAX_BYTES], stored)
        except ValueError:  # a malformed stored hash never matches
            return False


_hasher = PasswordHash((
    Argon2Hasher(time_cost=2, memory_cost=19456, parallelism=1),
    _MigratedBcrypt(),
))


def _concurrency() -> int:
    try:
        return max(1, int(os.environ.get("HUBZOID_PASSWORD_HASH_CONCURRENCY", "4")))
    except ValueError:
        return 4


_slots = threading.BoundedSemaphore(_concurrency())

# Verified against when there is no account or no password, so a sign-in for an
# unknown email takes as long as a wrong password for a real one.
_dummy_lock = threading.Lock()
_dummy_hash: str | None = None


def check(password) -> str:
    """The password, when it meets the rules. Raises PasswordRejected."""
    if not isinstance(password, str) or not password:
        raise PasswordRejected("Enter a password.")
    if len(password) < MIN_LENGTH:
        raise PasswordRejected(f"Use a password of at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordRejected(f"Use a password of at most {MAX_LENGTH} characters.")
    return password


def hash_password(password: str) -> str:
    """An Argon2id hash of a password that meets the rules."""
    check(password)
    with _slots:
        return _hasher.hash(password)


def verify_and_update(password: str, stored_hash: str | None) -> tuple[bool, str | None]:
    """(matches, new_hash). ``new_hash`` is set when the stored hash should be
    replaced: a migrated bcrypt hash, or Argon2 parameters that changed.

    An empty or unrecognised stored hash never matches, and still costs one
    hash computation. A password outside the length rules never matches."""
    if not isinstance(password, str) or not password or len(password) > MAX_LENGTH:
        dummy_verify()
        return False, None
    if not stored_hash:
        dummy_verify(password)
        return False, None
    try:
        with _slots:
            ok, updated = _hasher.verify_and_update(password, stored_hash)
    except UnknownHashError:
        dummy_verify(password)
        return False, None
    return bool(ok), (updated if ok else None)


def verify(password: str, stored_hash: str | None) -> bool:
    return verify_and_update(password, stored_hash)[0]


def dummy_verify(password: str = "hubzoid-no-account") -> None:
    """Spend the time of one verification without any account."""
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash("hubzoid-timing-equaliser")
    candidate = password if isinstance(password, str) and password else "x"
    with _slots:
        _hasher.verify(candidate[:MAX_LENGTH], _dummy_hash)


def is_supported_hash(stored_hash: str | None) -> bool:
    """True for an Argon2 or bcrypt hash this module can verify."""
    if not stored_hash:
        return False
    return any(h.identify(stored_hash) for h in _hasher.hashers)
