"""Two-step sign-in: authenticator codes (TOTP, RFC 6238) and recovery codes.

* One authenticator per person (``hz_mfa_factors``). Its secret is encrypted
  with the deployment key (``secretbox``) and decrypted only to check a code.
  Codes are six digits, 30-second steps, accepted one step either side. The
  highest step accepted is stored with a compare-and-set, so a code works once
  even when two requests race.
* Ten recovery codes (``hz_mfa_recovery``), stored as SHA-256 digests, each
  usable once. Making new ones replaces the old set.
* Challenges (``hz_auth_challenges``): a short-lived, single-use step bound to
  the account and its credential counter (``auth_epoch``), held by an HttpOnly
  cookie. ``mfa_login``: password (or provider) done, code needed.
  ``mfa_setup``: the same, for someone who must set MFA up first; it carries
  the new secret (encrypted). ``mfa_enroll``: a signed-in person setting it up
  from Settings. A challenge allows five wrong codes, then ends.

Codes are computed with the standard library (HMAC-SHA1), so there is no extra
dependency. The QR code is drawn as SVG by ``qrcode`` (BSD); the setup key is
always shown too, for apps that can't scan.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote

import sqlalchemy as sa

from .. import secretbox
from .schema import challenges, engine_for, mfa_factors, mfa_recovery, users

STEP = 30
DIGITS = 6
WINDOW = 1
CHALLENGE_SECONDS = 300
MAX_ATTEMPTS = 5
RECOVERY_CODES = 10
CHALLENGE_COOKIE = "hz_mfa"


class ChallengeInvalid(Exception):
    """The step expired, was used, belongs to a changed account, or had too
    many wrong codes."""


# ---- TOTP -------------------------------------------------------------------------

def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def _clean_code(code: str | None) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def matching_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step whose code is ``code`` (within the window), else None."""
    code = _clean_code(code)
    if len(code) != DIGITS:
        return None
    current = int((time.time() if now is None else now) // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}", safe="")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def qr_svg(uri: str) -> str:
    """The setup QR code as an SVG document (``data:`` URL ready)."""
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    svg = image.to_string(encoding="unicode")
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def format_secret(secret: str) -> str:
    """The setup key in groups of four, for typing into an app."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ---- factors -------------------------------------------------------------------------

def enrolled(hub_dir: Path, user_id: str) -> bool:
    with engine_for(Path(hub_dir)).connect() as conn:
        return conn.execute(sa.select(mfa_factors.c.user_id).where(
            mfa_factors.c.user_id == str(user_id))).first() is not None


def status(hub_dir: Path, user_id: str) -> dict:
    with engine_for(Path(hub_dir)).connect() as conn:
        factor = conn.execute(sa.select(mfa_factors.c.created_at).where(
            mfa_factors.c.user_id == str(user_id))).first()
        left = conn.execute(sa.select(sa.func.count()).select_from(mfa_recovery).where(
            mfa_recovery.c.user_id == str(user_id), mfa_recovery.c.used_at.is_(None))).scalar()
    return {"enabled": factor is not None, "since": factor[0] if factor else None,
            "recovery_codes_left": int(left or 0) if factor else 0}


def verify_code(hub_dir: Path, user_id: str, code: str, now: float | None = None) -> bool:
    """Check an authenticator code and consume its time step (once only)."""
    engine = engine_for(Path(hub_dir))
    with engine.connect() as conn:
        row = conn.execute(sa.select(mfa_factors.c.secret, mfa_factors.c.last_step).where(
            mfa_factors.c.user_id == str(user_id))).first()
    if row is None:
        return False
    try:
        secret = secretbox.decrypt_text(Path(hub_dir), row[0])
    except Exception:  # noqa: BLE001 - an unreadable secret signs nobody in
        return False
    step = matching_step(secret, code, now)
    if step is None or step <= int(row[1] or 0):
        return False
    with engine.begin() as conn:
        return bool(conn.execute(mfa_factors.update().where(
            mfa_factors.c.user_id == str(user_id), mfa_factors.c.last_step < step,
        ).values(last_step=step)).rowcount)


def _recovery_digest(code: str) -> str:
    clean = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return hashlib.sha256(clean.encode("ascii", "ignore")).hexdigest()


def _new_recovery_codes() -> list[str]:
    out = []
    for _ in range(RECOVERY_CODES):
        raw = base64.b32encode(secrets.token_bytes(10)).decode("ascii")
        out.append("-".join(raw[i:i + 4] for i in range(0, 16, 4)))
    return out


def _store_recovery(conn, user_id: str, codes: list[str], now: float) -> None:
    conn.execute(mfa_recovery.delete().where(mfa_recovery.c.user_id == str(user_id)))
    for code in codes:
        conn.execute(mfa_recovery.insert().values(code_hash=_recovery_digest(code),
                                                  user_id=str(user_id), created_at=now))


def use_recovery_code(hub_dir: Path, user_id: str, code: str) -> bool:
    with engine_for(Path(hub_dir)).begin() as conn:
        return bool(conn.execute(mfa_recovery.update().where(
            mfa_recovery.c.code_hash == _recovery_digest(code),
            mfa_recovery.c.user_id == str(user_id), mfa_recovery.c.used_at.is_(None),
        ).values(used_at=time.time())).rowcount)


def activate(hub_dir: Path, user_id: str, secret: str, confirmed_step: int) -> list[str]:
    """Save ``secret`` as the person's authenticator (replacing any) and return
    ten new recovery codes, shown once. ``confirmed_step`` is the step of the
    code that confirmed it, which can't be used again."""
    now = time.time()
    codes = _new_recovery_codes()
    sealed = secretbox.encrypt(Path(hub_dir), secret)
    with engine_for(Path(hub_dir)).begin() as conn:
        conn.execute(mfa_factors.delete().where(mfa_factors.c.user_id == str(user_id)))
        conn.execute(mfa_factors.insert().values(user_id=str(user_id), secret=sealed,
                                                 created_at=now, last_step=int(confirmed_step)))
        _store_recovery(conn, user_id, codes, now)
    return codes


def new_recovery_codes(hub_dir: Path, user_id: str) -> list[str]:
    codes = _new_recovery_codes()
    with engine_for(Path(hub_dir)).begin() as conn:
        if conn.execute(sa.select(mfa_factors.c.user_id).where(
                mfa_factors.c.user_id == str(user_id))).first() is None:
            raise KeyError(user_id)
        _store_recovery(conn, user_id, codes, time.time())
    return codes


def remove(hub_dir: Path, user_id: str, conn=None) -> bool:
    """Turn two-step sign-in off for the person (their authenticator and codes)."""
    def run(c) -> bool:
        gone = c.execute(mfa_factors.delete().where(mfa_factors.c.user_id == str(user_id))).rowcount
        c.execute(mfa_recovery.delete().where(mfa_recovery.c.user_id == str(user_id)))
        c.execute(challenges.delete().where(challenges.c.user_id == str(user_id),
                                            challenges.c.purpose.like("mfa%")))
        return bool(gone)

    if conn is not None:
        return run(conn)
    with engine_for(Path(hub_dir)).begin() as c:
        return run(c)


# ---- challenges ---------------------------------------------------------------------

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_challenge(hub_dir: Path, user: dict, purpose: str, data: dict | None = None,
                    seconds: int = CHALLENGE_SECONDS) -> str:
    """A new single-use step for ``user``; returns the token for its cookie.
    Earlier unfinished steps of the same purpose end."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    payload = dict(data or {})
    if "secret" in payload:
        payload["secret"] = secretbox.encrypt(Path(hub_dir), payload["secret"])
    with engine_for(Path(hub_dir)).begin() as conn:
        conn.execute(challenges.delete().where(challenges.c.user_id == str(user["id"]),
                                               challenges.c.purpose == purpose))
        conn.execute(challenges.insert().values(
            token_hash=_digest(token), user_id=str(user["id"]), purpose=purpose,
            auth_epoch=int(user.get("auth_epoch") or 0), data=json.dumps(payload),
            created_at=now, expires_at=now + seconds, attempts=0, used_at=None))
    return token


def read_challenge(hub_dir: Path, token: str | None, purposes: tuple[str, ...]) -> dict:
    """The live step behind ``token``: ``{"user_id", "purpose", "data", ...}``.
    Raises ChallengeInvalid when it expired, was used, has too many wrong
    codes, or its account changed (credential counter moved, not active)."""
    if not token or len(token) > 256:
        raise ChallengeInvalid()
    with engine_for(Path(hub_dir)).connect() as conn:
        row = conn.execute(
            sa.select(challenges, users.c.auth_epoch.label("current_epoch"),
                      users.c.status.label("status"))
            .select_from(challenges.join(users, users.c.id == challenges.c.user_id))
            .where(challenges.c.token_hash == _digest(token))).first()
    if row is None:
        raise ChallengeInvalid()
    m = row._mapping
    if (m["purpose"] not in purposes or m["used_at"] is not None or m["expires_at"] <= time.time()
            or m["attempts"] >= MAX_ATTEMPTS or int(m["current_epoch"] or 0) != int(m["auth_epoch"])
            or m["status"] != "active"):
        raise ChallengeInvalid()
    data = json.loads(m["data"] or "{}")
    if "secret" in data:
        data["secret"] = secretbox.decrypt_text(Path(hub_dir), data["secret"])
    return {"user_id": m["user_id"], "purpose": m["purpose"], "data": data,
            "expires_at": m["expires_at"], "attempts": m["attempts"], "token": token}


def wrong_code(hub_dir: Path, token: str) -> int:
    """Count a wrong code; returns how many tries are left (0: the step ended)."""
    with engine_for(Path(hub_dir)).begin() as conn:
        conn.execute(challenges.update().where(challenges.c.token_hash == _digest(token))
                     .values(attempts=challenges.c.attempts + 1))
        row = conn.execute(sa.select(challenges.c.attempts).where(
            challenges.c.token_hash == _digest(token))).first()
    return max(0, MAX_ATTEMPTS - int(row[0] if row else MAX_ATTEMPTS))


def finish_challenge(hub_dir: Path, token: str) -> bool:
    """Mark the step used. False when another request used it first."""
    with engine_for(Path(hub_dir)).begin() as conn:
        return bool(conn.execute(challenges.update().where(
            challenges.c.token_hash == _digest(token), challenges.c.used_at.is_(None),
        ).values(used_at=time.time())).rowcount)
